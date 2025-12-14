import os
import time
import requests
import threading
import signal
import sys
from collections import deque
from datetime import datetime
from flask import Flask, render_template_string

# ================= 1. 环境配置 =================
WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")
TARGET_SKU_ATTR_ID = int(os.getenv("TARGET_SKU_ATTR_ID", "7711440"))
TARGET_PRODUCT_NAME = os.getenv("TARGET_PRODUCT_NAME", "水洗黑")
STORE_ID = os.getenv("STORE_ID", "1272")
PRODUCT_ID = os.getenv("PRODUCT_ID", "213743")
PORT = int(os.getenv("PORT", 8080))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
USER_COOKIE = os.getenv("USER_COOKIE", "")

API_URL = f"https://shopapi.haomaitong.com/v2/product/{PRODUCT_ID}/sku?storeId={STORE_ID}"

# ================= 2. 全局状态 =================
log_queue = deque(maxlen=50)
last_status = "初始化启动..."
last_check_time = "等待首次运行"
# 初始设为 0，这样第一次检测到有货(比如5)时，5>0 会触发补货通知
last_stock_count = 0 
last_error_time = None

# ================= 3. 工具函数 =================
def add_log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    log_queue.appendleft(entry)

def send_wecom(content):
    if not WECOM_WEBHOOK_URL: return
    try:
        data = {"msgtype": "text", "text": {"content": content, "mentioned_list": ["@all"]}}
        requests.post(WECOM_WEBHOOK_URL, json=data, timeout=5)
    except Exception as e:
        print(f"❌ 推送失败: {e}")

def handle_error_notify(error_msg):
    global last_error_time
    current_time = datetime.now()
    add_log(f"❌ 异常: {error_msg}")
    
    # 报错静默期 30分钟
    if last_error_time is None or (current_time - last_error_time).total_seconds() > 1800:
        send_wecom(f"⚠️ 监控报警\n原因：{error_msg}\n(此类报错30分钟内静默)")
        last_error_time = current_time

# ================= 4. 退出信号处理 =================
def graceful_exit(signum, frame):
    msg = f"🛑 监控服务停止 (Signal {signum})"
    print(msg)
    send_wecom(msg)
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_exit)
signal.signal(signal.SIGINT, graceful_exit)

# ================= 5. 核心监控逻辑 (重点修改) =================
def monitor_loop():
    global last_status, last_check_time, last_stock_count
    
    add_log(f"🚀 启动 | 商品: {TARGET_PRODUCT_NAME} ({TARGET_SKU_ATTR_ID})")
    send_wecom(f"🟢 监控已部署\n目标：{TARGET_PRODUCT_NAME}\n策略：库存变动即推送")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) XWEB/17071",
        "Content-Type": "application/json;charset=utf-8",
        "X-StoreId": STORE_ID,
        "X-ClientType": "weapp"
    }
    if USER_COOKIE: headers["Cookie"] = USER_COOKIE

    while True:
        try:
            last_check_time = datetime.now().strftime("%H:%M:%S")
            resp = requests.get(API_URL, headers=headers, timeout=10)
            
            if resp.status_code != 200:
                handle_error_notify(f"HTTP Error {resp.status_code}")
                time.sleep(CHECK_INTERVAL)
                continue

            data = resp.json()
            if data.get("code") != 200:
                handle_error_notify(f"API 拒绝: {data.get('message')}")
                time.sleep(CHECK_INTERVAL)
                continue

            sku_list = data.get("data", {}).get("skuList", [])
            found = False

            for sku in sku_list:
                if TARGET_SKU_ATTR_ID in sku.get("validProductAttrValueIdList", []):
                    found = True
                    current_count = sku.get("count", 0)
                    price = sku.get("price", 0)
                    
                    # === 库存变动判断逻辑 ===
                    if current_count > 0:
                        # 情况1：上次没货(0)，这次有货(>0) -> 大补货
                        if last_stock_count == 0:
                            msg = f"🎉 终于补货啦！\n商品：{TARGET_PRODUCT_NAME}\n库存：{current_count}\n价格：{price}"
                            add_log(f"🚨 补货: {current_count}")
                            send_wecom(msg)
                            last_status = f"补货 (库存: {current_count})"
                        
                        # 情况2：库存变多了 (上次5, 这次10) -> 二次上架
                        elif current_count > last_stock_count:
                            diff = current_count - last_stock_count
                            msg = f"📈 库存增加了！(+{diff})\n当前：{current_count}\n上次：{last_stock_count}"
                            add_log(f"🚨 增加: {last_stock_count}->{current_count}")
                            send_wecom(msg)
                            last_status = f"加库 (库存: {current_count})"

                        # 情况3：库存变少了 (上次10, 这次8) -> 被人买了，紧迫感！
                        elif current_count < last_stock_count:
                            diff = last_stock_count - current_count
                            msg = f"📉 库存减少了！(-{diff})\n被人买走啦，快冲！\n当前：{current_count}"
                            add_log(f"🚨 减少: {last_stock_count}->{current_count}")
                            send_wecom(msg)
                            last_status = f"被抢 (库存: {current_count})"
                        
                        # 情况4：库存没变 -> 保持安静
                        else:
                            if last_status != f"库存滞留 ({current_count})":
                                add_log(f"👀 库存未变: {current_count}")
                            last_status = f"库存滞留 ({current_count})"

                    else:
                        # 情况5：从有货变成了无货(0) -> 售罄通知
                        if last_stock_count > 0:
                            msg = f"❌ 哎呀，卖光了！\n库存归零"
                            add_log("📉 售罄")
                            send_wecom(msg)
                            last_status = "已售罄"
                        else:
                            last_status = "无货监控中..."
                    
                    # 只有在成功获取数据后，才更新记忆值
                    last_stock_count = current_count

            if not found:
                handle_error_notify(f"未找到SKU ID {TARGET_SKU_ATTR_ID}")

        except Exception as e:
            handle_error_notify(f"循环异常: {str(e)}")
        
        time.sleep(CHECK_INTERVAL)

# ================= 6. Web 服务 =================
app = Flask(__name__)
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>库存监控</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, sans-serif; padding: 20px; background: #f4f6f8; max-width: 800px; margin: 0 auto; }
        .card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); margin-bottom: 20px; }
        .status { color: #059669; font-weight: bold; }
        .logs { background: #111827; color: #d1d5db; padding: 15px; border-radius: 8px; height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; }
        .log-line { border-bottom: 1px solid #374151; padding: 2px 0; }
        .btn { display: inline-block; padding: 8px 16px; background: #2563eb; color: white; text-decoration: none; border-radius: 6px; font-size: 14px; margin-top: 10px;}
    </style>
    <script>setTimeout(function(){ location.reload(); }, 10000);</script>
</head>
<body>
    <div class="card">
        <h3>📊 监控面板</h3>
        <p>商品：<strong>{{ name }}</strong></p>
        <p>状态：<span class="status">{{ status }}</span></p>
        <p>时间：{{ time }}</p>
        <p>当前记录库存：<strong>{{ stock }}</strong></p>
        <a href="/" class="btn">刷新页面</a>
    </div>
    <div class="card">
        <h3>📝 运行日志</h3>
        <div class="logs">
            {% for log in logs %}
            <div class="log-line">{{ log }}</div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML, name=TARGET_PRODUCT_NAME, status=last_status, 
                                  time=last_check_time, stock=last_stock_count, logs=list(log_queue))

@app.route('/health')
def health(): return "OK", 200

if __name__ == "__main__":
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=PORT)
