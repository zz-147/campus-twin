# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Campus Twin AI 运维 Agent（FastAPI :8100，LangGraph + RAG + MCP 工具）
# 构建：docker build -t campus-twin/agent:local ./agent
# 运行：见根目录 docker-compose.yml；LLM key 通过环境变量注入，不写入镜像
#
# 说明：本机未安装 Docker，本文件与 compose 仅做静态校验，未在本机 build/run 实测。
# ---------------------------------------------------------------------------
FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AGENT_API_HOST=0.0.0.0 \
    AGENT_API_PORT=8100 \
    AGENT_DB_PATH=/app/data/agent.db \
    AGENT_GRAPH_DB_PATH=/app/data/graph.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 只拷贝应用代码；.env / data/ 不入镜像（compose 用环境变量 + volume）
COPY app ./app

RUN mkdir -p /app/data

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8100"]
