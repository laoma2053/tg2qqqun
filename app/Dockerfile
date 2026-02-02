FROM python:3.11-slim

WORKDIR /app

# 启用不缓冲输出，避免 docker logs 看不到启动日志
ENV PYTHONUNBUFFERED=1

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝代码
COPY . /app

# 启动
# 默认运行转发器：python main.py
# 首次登录可运行：python login.py（生成 /session/telegram.session）
CMD ["python", "main.py"]
