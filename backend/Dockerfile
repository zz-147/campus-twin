# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Campus Twin 后端（FastAPI :8000，采集 + 检测 + 告警 + 场景/路径追踪）
# 构建：docker build -t campus-twin/backend:local ./backend
# 运行：见根目录 docker-compose.yml；真实 PT NC 不在容器内，详见 docs/部署与CI.md
#
# 说明：本机未安装 Docker，本文件与 compose 仅做静态校验（YAML/路径/端口/依赖），
#       未在本机 build/run 实测。
# ---------------------------------------------------------------------------
FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/app/data/campus_twin.db

WORKDIR /app

# 先安装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 只拷贝应用代码；data/ 由 compose volume 挂载（不把本机 SQLite 打进镜像）
COPY app ./app

RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
