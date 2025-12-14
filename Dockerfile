# 使用轻量级的 Python 基础镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置时区为上海 (解决日志时间问题)
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复制依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制程序代码
COPY main.py .

# 暴露端口 (虽然 Serverless 主要是通过环境变量 PORT 控制，但写一下是个好习惯)
EXPOSE 8080

# 启动命令
CMD ["python", "main.py"]
