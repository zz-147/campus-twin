# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Campus Twin MCP Bridge（Python 3.14 + mcp 1.30；HTTP :8200 → stdio 外部 MCP servers）
# 构建：docker build -t campus-twin/mcp-bridge:local ./mcp_bridge
# 运行：见根目录 docker-compose.yml
#
# 说明：本机未安装 Docker，本文件与 compose 仅做静态校验，未在本机 build/run 实测。
#       - 本地 venv 为 Python 3.14；官方 python:3.14-slim 镜像发布时间可能滞后，
#         若构建拉不到 3.14-slim，可改 python:3.13-slim（mcp 1.30 仅要求 >=3.10）。
#       - bridge 的 config.json 里是 Windows 绝对路径 + npx 命令；镜像构建时用 Python
#         做字符串改写适配容器路径（不改 bridge.py 源码）。
#       - 外部 filesystem/memory MCP servers 由 npx 在运行时首次拉取（需要网络），
#         compose 把 configs/、docs/ 只读挂载进容器供 filesystem server 使用。
# ---------------------------------------------------------------------------
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_BRIDGE_CONFIG=/app/config.json \
    NPM_CONFIG_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

# Node/npm：bridge 通过 npx 拉起外部 MCP servers（filesystem / memory）
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py config.json ./

# 容器路径适配：D:\zdsh\campus-twin\... → /app/...
RUN python -c "import json,pathlib;p=pathlib.Path('/app/config.json');d=json.loads(p.read_text(encoding='utf-8'));f=lambda s:s.replace(chr(92),'/').replace('D:','').replace('/zdsh/campus-twin/mcp_bridge','/app').replace('/zdsh/campus-twin','/app');[s.update({'args':[f(a) for a in s.get('args',[])],'env':{k:(f(v) if isinstance(v,str) else v) for k,v in (s.get('env') or {}).items()}}) for s in d['servers']];p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')"

RUN mkdir -p /app/data /app/logs /app/configs /app/docs

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/health', timeout=3)"

# bridge.py 的 __main__ 强制只监听 127.0.0.1（安全限制，容器内不可达）；
# 这里直接用同等的 uvicorn 工厂启动并绑定 0.0.0.0，由 compose 只发布到宿主机 127.0.0.1。
CMD ["python", "-c", "from bridge import create_app; import uvicorn; uvicorn.run(create_app(), host='0.0.0.0', port=8200, log_level='warning')"]
