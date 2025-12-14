import os
import time
import requests
import threading
import signal
import sys
from collections import deque
from datetime import datetime
from flask import Flask, render_template_string

# ================= 1. 环境配置 (Environment Variables) =================
# 核心配置
WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")
TARGET_SKU_ATTR_ID = int(os.getenv("TARGET_SKU_ATTR_ID", "7711440"))
TARGET_PRODUCT_NAME = os.getenv("TARGET_PRODUCT_NAME", "水洗黑")
STORE_ID = os.getenv("STORE_ID", "1272")
PRODUCT_ID = os.getenv("PRODUCT_ID", "213743")

# 运行参数
PORT = int(os.getenv("PORT", 8080))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60")) # 默认间隔 60秒
USER_COOKIE = os.getenv("USER_COOKIE", "")

# API 地址
API_URL = f"https://shopapi.haomaitong.com/v2/product/{PRODUCT_ID}/sku?storeId={STORE_ID}"

# ================= 2. 全局状态记录 =================
log_queue = deque(maxlen=50) # 只保留最近50条日志
last_status = "初始化启动..."
last_check_time = "等待首次运行"
last_stock_count = 0  # 记录上一次的库存，初始为0
last_error_time = None # 用于报错静默

# ================= 3. 工具函数 =================
def add_log(message):
    """写入日志到队列并打印到控制台"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    log_queue.appendleft(entry)
    return entry

def send_wecom(content):
    """发送企业微信通知"""
    if not WECOM_WEBHOOK_URL:
        return
    try:
        # 发送给所有人
        data = {"msgtype": "text", "text": {"content": content, "mentioned_list": ["@all"]}}
        requests.post(WECOM_WEBHOOK_URL, json=data, timeout=5)
    except Exception as e:
        print(f"❌ 推送失败: {e}")

def handle_error_notify(error_msg):
    """处理异常报错（含30分钟静默机制）"""
    global last_error_time
    current_time = datetime.now()
    
    add_log(f"❌ 异常捕获: {error_msg}")
    
    # 如果从未报错，或者距离上次报错超过 30 分钟 (1800秒)
    if last_error_time is None or (current_time - last_error_time).total_seconds() > 1800:
        send_wecom(f"⚠️ 监控报警\n原因：{error_msg}\n(此类报错30分钟内不再重复推送)")
        last_error_time = current_time

# ================= 4. 优雅退出 (Signal Handling) =================
def graceful_exit(signum, frame):
    """捕获 Docker 停止或 Ctrl+C 信号"""
    msg = f"🛑 监控服务正在停止 (Signal {signum})"
    print(msg)
    send_wecom(msg)
    sys.exit(0)

# 注册信号
signal.signal(signal.SIGTERM, graceful_exit)
signal.signal(signal.SIGINT, graceful_exit)

# ================= 5. 核心监控逻辑 =================
def monitor_loop():
    global last_status, last_check_time, last_stock_count
    
    add_log(f"🚀 监控线程启动 | 目标: {TARGET_PRODUCT_NAME} (ID: {TARGET_SKU_ATTR_ID})")
    send_wecom(f"🟢 监控服务已部署\n目标：{TARGET_PRODUCT_NAME}\n策略：库存变动即推送 (无冷却)")

    # 构造请求头
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) XWEB/17071",
        "Content-Type": "application/json;charset=utf-8",
        "X-StoreId": STORE_ID,
        "X-ClientType": "weapp"
    }
    if USER_COOKIE:
        headers["Cookie"] = USER_COOKIE

    while True:
        try:
            last_check_time = datetime.now().strftime("%H:%M:%S")
            resp = requests.get(API_URL, headers=headers, timeout=10)
            
            # 1. 检查 HTTP 状态
            if resp.status_code != 200:
                handle_error_notify(f"HTTP Error {resp.status_code}")
                time.sleep(CHECK_INTERVAL)
                continue

            # 2. 检查 API 业务状态
            data = resp.json()
            if data.get("code") != 200:
                handle_error_notify(f"API 拒绝: {data.get('message', '未知错误')}")
                time.sleep(CHECK_INTERVAL)
                continue

            sku_list = data.get("data", {}).get("skuList", [])
            found = False

            # 3. 遍历查找目标 SKU
            for sku in sku_list:
                if TARGET_SKU_ATTR_ID in sku.get("validProductAttrValueIdList", []):
                    found = True
                    current_count = sku.get("count", 0)
                    price = sku.get("price", 0)
                    
                    # === 库存变动判断逻辑 ===
                    if current_count > 0:
                        # 情况A：从无货(0)变有货(>0) -> 大补货
                        if last_stock_count == 0:
                            msg = f"🎉 终于补货啦！\n商品：{TARGET_PRODUCT_NAME}\n库存：{current_count}\n价格：{price}"
                            add_log(f"🚨 检测到补货: {current_count}")
                            send_wecom(msg)
                            last_status = f"补货 (库存: {current_count})"
                        
                        # 情况B：库存变多了 (商家加库存)
                        elif current_count > last_stock_count:
                            diff = current_count - last_stock_count
                            msg = f"📈 库存增加了！(+{diff})\n当前：{current_count}\n上次：{last_stock_count}"
                            add_log(f"🚨 库存增加: {last_stock_count}->{current_count}")
                            send_wecom(msg)
                            last_status = f"加库 (库存: {current_count})"

                        # 情况C：库存变少了 (被抢购)
                        elif current_count < last_stock_count:
                            diff = last_stock_count - current_count
                            msg = f"📉 库存减少了！(-{diff})\n有人买走啦，快冲！\n当前：{current_count}"
                            add_log(f"🚨 库存减少: {last_stock_count}->{current_count}")
                            send_wecom(msg)
                            last_status = f"被抢 (库存: {current_count})"
                        
                        # 情况D：库存没变 -> 打印日志心跳，但不推送
                        else:
                            add_log(f"👀 监控中... 库存: {current_count} (未变)")
                            last_status = f"库存滞留 ({current_count})"

                    else:
                        # 情况E：从有货变无货(0) -> 售罄
                        if last_stock_count > 0:
                            msg = f"❌ 哎呀，卖光了！\n库存归零"
                            add_log("📉 已售罄")
                            send_wecom(msg)
                            last_status = "已售罄"
                        else:
                            # 持续无货
                            add_log("💤 暂时无货...")
                            last_status = "无货监控中..."
                    
                    # 更新库存记忆
                    last_stock_count = current_count

            if not found:
                handle_error_notify(f"未找到目标 SKU ID: {TARGET_SKU_ATTR_ID}")

        except Exception as e:
            handle_error_notify(f"循环运行异常: {str(e)}")
        
        # 等待下一次检查 (默认60秒)，不再有额外的 5 分钟等待
        time.sleep(CHECK_INTERVAL)

# ================= 6. Web 服务 (Flask) =================
app = Flask(__name__)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>库存监控</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 20px; background: #f4f6f8; max-width: 800px; margin: 0 auto; color: #333; }
        .card { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 20px; }
        h3 { margin-top: 0; font-size: 1.2rem; display: flex; align-items: center; }
        .status { color: #059669; font-weight: bold; font-size: 1.1rem; }
        .logs { background: #1e293b; color: #e2e8f0; padding: 15px; border-radius: 8px; height: 350px; overflow-y: auto; font-family: 'Menlo', 'Monaco', 'Courier New', monospace; font-size: 13px; line-height: 1.6; }
        .log-line { border-bottom: 1px solid #334155; padding: 2px 0; }
        .btn { display: inline-block; padding: 10px 20px; background: #2563eb; color: white; text-decoration: none; border-radius: 6px; font-weight: 500; margin-top: 15px; transition: background 0.2s; }
        .btn:hover { background: #1d4ed8; }
        .info-row { display: flex; justify-content: space-between; margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; }
        .info-label { color: #666; }
        .info-val { font-weight: 600; }
    </style>
    <script>
        // 每 10 秒自动刷新网页，查看最新日志
        setTimeout(function(){ location.reload(); }, 10000);
    </script>
</head>
<body>
    <div class="card">
        <h3>📊 监控面板</h3>
        <div class="info-row">
            <span class="info-label">商品名称</span>
            <span class="info-val">{{ name }}</span>
        </div>
        <div class="info-row">
            <span class="info-label">当前状态</span>
            <span class="status">{{ status }}</span>
        </div>
        <div class="info-row">
            <span class="info-label">最后检查时间</span>
            <span class="info-val">{{ time }}</span>
        </div>
        <div class="info-row">
            <span class="info-label">当前记录库存</span>
            <span class="info-val" style="font-size: 1.2em; color: #2563eb;">{{ stock }}</span>
        </div>
        <a href="/" class="btn">刷新页面</a>
    </div>

    <div class="card">
        <h3>📝 实时日志 (最近50条)</h3>
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
    return render_template_string(HTML_TEMPLATE, 
                                  name=TARGET_PRODUCT_NAME, 
                                  status=last_status, 
                                  time=last_check_time, 
                                  stock=last_stock_count, 
                                  logs=list(log_queue))

@app.route('/health')
def health():
    return "OK", 200

if __name__ == "__main__":
    # 1. 启动监控线程 (守护线程)
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    
    # 2. 启动 Flask Web 服务
    print(f"🌍 Web 服务正在启动，监听端口: {PORT}")
    app.run(host='0.0.0.0', port=PORT)
