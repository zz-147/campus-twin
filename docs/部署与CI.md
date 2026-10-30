# 部署与 CI（W15-P2）

本文说明 Campus Twin 的三种运行方式：**本机直跑**（已实测）、**Docker Compose**（已写好并提供
静态校验，但**本机未安装 Docker，未实测**）、**GitHub Actions 离线 CI**（workflow 已写好，需
推送到 GitHub 后实际运行）。

| 方式 | 状态 | 说明 |
|---|---|---|
| 本机直跑（Python venv + npm） | ✅ 已在开发机实测 | 见下方「一、本机直跑」 |
| Docker Compose（4 容器） | ⚠️ **未本机实测**（本机无 Docker） | 已做 YAML/路径/端口/依赖静态校验；见「二、Docker Compose」 |
| GitHub Actions CI（6 job） | ⚠️ **未本地执行**（需 push 后 GitHub 跑） | 见「三、GitHub Actions」 |

---

## 一、本机直跑（推荐；真实 PT NC 只支持这种方式）

### 0) 前置

| 组件 | 版本 / 说明 |
|---|---|
| Python venv | `D:\zdsh\campus-twin\.venv`（Python 3.9，backend/agent） |
| MCP venv | `D:\zdsh\campus-twin\.venv-mcp314`（Python 3.14，mcp 1.30，mcp_server / mcp_bridge） |
| Node / npm | Node 20+（frontend / feishu-bot；npm 可用 npmmirror） |
| Packet Tracer 9 | 打开 `pt\campus-final.pkt`，NC 开启 Real World Access（`127.0.0.1:58000`） |

### 1) 三个常驻服务

```powershell
# 后端 :8000
cd D:\zdsh\campus-twin\backend
D:\zdsh\campus-twin\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# AI Agent :8100（读取 agent\.env；默认 DeepSeek，可切 mock 离线）
cd D:\zdsh\campus-twin\agent
D:\zdsh\campus-twin\.venv\Scripts\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8100

# 前端 :5173
cd D:\zdsh\campus-twin\frontend
npm run dev -- --port 5173
```

### 2) 可选组件

```powershell
# MCP Bridge :8200（Agent 的 filesystem/memory 外部工具；需 Node npx）
cd D:\zdsh\campus-twin\mcp_bridge
D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe bridge.py --port 8200

# MCP Server（stdio，不占端口；给 Claude Desktop / Cursor 用）
D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe D:\zdsh\campus-twin\mcp_server\server.py

# 飞书机器人（长连接，需要飞书应用凭据，放 feishu-bot\.env）
cd D:\zdsh\campus-twin\feishu-bot
npm start
```

验证：

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8100/api/health
curl.exe http://127.0.0.1:8200/health
```

---

## 二、Docker Compose（未本机实测）

### 2.1 文件清单

| 文件 | 作用 |
|---|---|
| `docker-compose.yml`（根目录） | 4 服务编排：backend / agent / frontend / mcp_bridge + 命名 volume + healthcheck + depends_on |
| `backend/Dockerfile` | `python:3.9-slim` → uvicorn `app.main:app` :8000 |
| `agent/Dockerfile` | `python:3.9-slim` → uvicorn `app.api:app` :8100 |
| `frontend/Dockerfile` | `node:20-alpine` 构建 → `nginx:1.27-alpine` 托管 :80（SPA fallback） |
| `mcp_bridge/Dockerfile` | `python:3.14-slim` + Node/npm（npx）→ 容器内 0.0.0.0:8200 |
| `.github/workflows/ci.yml` | 6 个离线 CI job |

### 2.2 启动

前置：Docker Desktop（含 Compose v2）。

```powershell
cd D:\zdsh\campus-twin

# 首次构建 + 启动（4 个镜像）
docker compose up -d --build

# 查看状态 / 健康检查
docker compose ps
docker compose logs -f backend

# 打开 http://127.0.0.1:5173/
# API：8000 backend / 8100 agent / 8200 mcp_bridge

# 停止（保留数据 volume）
docker compose down

# 停止并清空数据库 volume（⚠️ 会删容器内数据，不会碰本机 backend/data）
docker compose down -v
```

单独构建：

```powershell
docker build -t campus-twin/backend:local   ./backend
docker build -t campus-twin/agent:local     ./agent
docker build -t campus-twin/frontend:local  ./frontend
docker build -t campus-twin/mcp-bridge:local ./mcp_bridge
```

### 2.3 端口与 volume

| 服务 | 容器端口 | 宿主机 | Volume（容器路径） | 说明 |
|---|---|---|---|---|
| backend | 8000 | `127.0.0.1:8000` | `backend_data:/app/data` | SQLite 持久化 |
| agent | 8100 | `127.0.0.1:8100` | `agent_data:/app/data` | agent.db / graph.db |
| frontend | 80 | `127.0.0.1:5173` | — | nginx 静态资源 |
| mcp_bridge | 8200 | `127.0.0.1:8200` | `bridge_data:/app/data`、`bridge_logs:/app/logs`，`./configs`、`./docs` 只读挂载 | filesystem 白名单目录 |

所有端口只绑定宿主机 `127.0.0.1`，不暴露到局域网。

### 2.4 环境变量（不写密钥）

compose 通过 `${VAR:-默认值}` 注入，密钥不落文件；可在根目录 `.env`（已被 `.gitignore` 排除）
或 shell 中覆盖：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DRIVER` | `simulator` | 容器内默认仿真；真实 NC 见 2.6 |
| `NC_BASE_URL` / `NC_USERNAME` / `NC_PASSWORD` | `http://host.docker.internal:58000/api/v1` / `admin` / `cisco` | PT NC 占位（容器内通常不可达；Linux 宿主已通过 `extra_hosts: host.docker.internal:host-gateway` 解析） |
| `LLM_PROVIDER` | `mock` | 默认离线；切 `deepseek` 等时配合下面变量 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 空 | **真实密钥只从环境变量/.env 注入** |
| `AGENT_DATA_SOURCE` / `AGENT_ENGINE` | `http` / `langgraph` | Agent 数据源与编排引擎 |
| `VITE_API_BASE` / `VITE_AGENT_BASE` | `http://127.0.0.1:8000` / `http://127.0.0.1:8100` | 浏览器侧地址（build args，改后需重新 build） |
| `NPM_CONFIG_REGISTRY` | `https://registry.npmmirror.com` | bridge 容器 npx 拉包镜像 |

示例（本地演示，不提交）：

```powershell
$env:LLM_PROVIDER='deepseek'
$env:LLM_API_KEY='sk-...'   # 只存在当前 shell
docker compose up -d --build agent
```

### 2.5 健康检查与启动顺序

- 4 个服务均配置 `healthcheck`：
  - backend：`python -c urllib http://127.0.0.1:8000/api/health`
  - agent：`...:8100/api/health`（`start_period: 30s`，等 LangGraph/SQLite 初始化）
  - mcp_bridge：`...:8200/health`
  - frontend：`wget http://127.0.0.1/`
- `agent.depends_on.backend = service_healthy`；`agent.depends_on.mcp_bridge = service_started`
  （bridge 不可用时 Agent 仍可启动，MCP 工具优雅降级）；
- mcp_bridge 首次启动时 `npx -y ...` 会下载外部 MCP servers（取决于网络，可能 1-2 分钟），
  因此其 healthcheck 特意放宽为 `timeout 10s / retries 10 / start_period 120s`；
  如果长时间不 healthy，先看 `docker compose logs mcp_bridge` 是否为 npm 网络问题。
  `frontend.depends_on.backend/agent = service_healthy`。
- 其余参数：`restart: unless-stopped`；interval 30s / timeout 5s / retries 5。

### 2.6 PT NC 与容器边界（重要）

**Packet Tracer 的 Network Controller 只监听宿主机 `127.0.0.1:58000`，不在容器内，也不能被
容器直接访问。** 因此：

1. **默认 `DRIVER=simulator`**：compose 不依赖 PT 也能独立跑通（仿真校园网 + 仿真指标）。
2. 想用真实 NC 数据时，推荐**本机直跑 backend**（见「一、本机直跑」），其余服务仍可容器化。
3. 可选方案（均未本机实测）：
   - Linux 宿主机：backend 容器使用 `network_mode: host`（Compose 网络需同步调整，
     Windows Docker Desktop 不支持该方案）。
   - 在宿主机起一个 TCP 转发（如 `socat TCP-LISTEN:58000,fork,reuseaddr TCP:127.0.0.1:58000`
     的变体）把容器侧连接代理到宿主 loopback；PT NC 看到的来源仍是本机。此方案需自行验证
     NC 是否接受代理连接。
   - 直接修改 compose 的 `NC_BASE_URL` 指向可达地址（要求 NC 允许非 loopback 客户端，PT 默认不允许）。
4. `flow-analysis`（路径追踪）、`/host`、`/assurance/health-issues` 等真实数据能力都依赖 NC，
   simulator 模式下路径追踪会返回“当前驱动不支持路径追踪/NC 不可用”的中文错误（设计如此）。

### 2.7 Docker 未实测清单（如实标注）

本机**未安装 Docker**，以下均**未执行**，仅静态校验：

- [ ] `docker build` 四个镜像（基础镜像拉取、apt/npm/pip 依赖安装、跨平台构建）
- [ ] `docker compose up` 与 4 个 healthcheck 的 healthy 转换
- [ ] bridge 容器内 `npx` 首次拉取 filesystem/memory MCP servers（需网络）
- [ ] frontend 容器内 `npm ci` / `vite build`（本机 npm build 已另行验证）
- [ ] 容器内访问真实 PT NC（见 2.6，默认不可达）
- [ ] volume 持久化与重启恢复

已静态校验（见「五、静态校验记录」）：YAML 可解析、4 服务/端口/healthcheck/depends_on/volume
齐全、build context 与 Dockerfile 路径存在、compose 无真实密钥、Dockerfile 关键指令（FROM/
COPY/EXPOSE/HEALTHCHECK/CMD）齐全。

---

## 三、GitHub Actions 离线 CI

### 3.1 触发与文件

- 文件：`.github/workflows/ci.yml`
- 触发：`push` / `pull_request`（`main`）+ 手动 `workflow_dispatch`
- 并发：同 ref 新 run 取消旧 run；权限仅 `contents: read`
- 徽章（README 已加）：

```markdown
[![CI](https://github.com/zz-147/campus-twin/actions/workflows/ci.yml/badge.svg)](https://github.com/zz-147/campus-twin/actions/workflows/ci.yml)
```

> 徽章/流水线需仓库 `zz-147/campus-twin` 推送后由 GitHub 实际运行，本地无法执行 Actions。

### 3.2 6 个 job（全部离线，不连真实 LLM / PT / NC）

| job | Runner | Python/Node | 步骤 | 预计耗时 |
|---|---|---|---|---|
| `backend` | ubuntu-latest | Python 3.9 | `pip install -r backend/requirements.txt` → `pytest backend/tests` | ~2 min |
| `agent` | ubuntu-latest | Python 3.9 | `pip install -r agent/requirements.txt` → `LLM_PROVIDER=mock` `pytest agent/tests` | ~5 min |
| `mcp_server` | ubuntu-latest | Python 3.12 | `pip install -r mcp_server/requirements.txt pytest` → `pytest mcp_server/tests` | ~2 min |
| `mcp_bridge` | ubuntu-latest | Python 3.13 | `pip install -r mcp_bridge/requirements.txt` → `unittest discover -s tests` | ~2 min |
| `feishu-bot` | ubuntu-latest | Node 20 | `npm ci` → `npm run self-test` | ~1 min |
| `frontend` | ubuntu-latest | Node 20 | `npm ci` → `npm run build`（不跑浏览器 smoke） | ~2 min |

版本说明：

- `mcp` 1.30 的 `Requires-Python` 是 **>=3.10**，所以 mcp_server / mcp_bridge 两个 job 用
  3.12 / 3.13；本机 mcp_bridge venv 为 3.14，若 GitHub runner 已提供 3.14，可把
  `python-version` 改为 `'3.14'`。
- 每个 job 均设置 `timeout-minutes`，Python/Node job 使用 `cache: pip` / `cache: npm`。
- 测试全部离线：backend 不连 NC（fixtures/mock）、agent 用 mock provider + fixture、
  mcp_server 用 MockTransport、mcp_bridge 不启动 npx servers。

### 3.3 本地等价命令（CI 前自检）

```powershell
# backend
cd D:\zdsh\campus-twin\backend; D:\zdsh\campus-twin\.venv\Scripts\python.exe -m pytest tests -q

# agent（离线）
cd D:\zdsh\campus-twin\agent
$env:LLM_PROVIDER='mock'; $env:AGENT_DATA_SOURCE='fixture'
D:\zdsh\campus-twin\.venv\Scripts\python.exe -m pytest tests -q

# mcp_server（项目主 venv 3.9 也可跑离线单测）
cd D:\zdsh\campus-twin\mcp_server; D:\zdsh\campus-twin\.venv\Scripts\python.exe -m pytest tests -q

# mcp_bridge
cd D:\zdsh\campus-twin\mcp_bridge; D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe -m unittest discover -s tests -v

# feishu-bot / frontend
cd D:\zdsh\campus-twin\feishu-bot; npm ci; npm run self-test
cd D:\zdsh\campus-twin\frontend; npm ci; npm run build
```

---

## 四、安全约定

1. **无密钥入库**：compose CI 均不包含真实 Key；`LLM_API_KEY` 使用空占位
   `${LLM_API_KEY:-}`，真实值只从环境变量/根目录 `.env`（gitignore）注入。
2. **只绑定本机**：4 个服务端口均 `127.0.0.1:...`，不暴露局域网；MCP Server 走 stdio 不占端口。
3. **只读边界不变**：Docker/CI 不改变后端/Agent 的鉴权与只读约束；mcp_bridge 仍只监听容器内网
   由 compose 发布，filesystem 目录只读挂载。
4. **数据不打包**：镜像不 COPY `backend/data`、`agent/data`、`mcp_bridge/data`、`.env`；
   容器数据在命名 volume 中，`down -v` 会删除容器数据（不影响本机目录）。
5. **版本固定**：mcp 1.30（不升 2.x，FastMCP 改名）；Python 3.9（backend/agent）/ 3.14（bridge）。

---

## 五、静态校验记录（2026-10-30，本机无 Docker）

使用项目主 venv 的 PyYAML 6.0.3 解析 + 结构脚本校验（不 build）：

```powershell
cd D:\zdsh\campus-twin
D:\zdsh\campus-twin\.venv\Scripts\python.exe -c "import yaml; ..."
```

结果：

| 校验项 | 结果 |
|---|---|
| `docker-compose.yml` YAML 解析 | ✅ 通过；`services` = backend/agent/frontend/mcp_bridge |
| 端口映射 8000/8100/5173/8200 | ✅ 全部存在且只绑定 `127.0.0.1` |
| healthcheck（4 服务） | ✅ 全部存在 |
| build context + Dockerfile 路径 | ✅ 4/4 存在 |
| depends_on（agent→backend，frontend→backend/agent） | ✅ 通过 |
| 命名 volume（backend_data/agent_data/bridge_data/bridge_logs） | ✅ 通过 |
| compose 是否含真实密钥 | ✅ 无（`sk-` 字面量 0 处；LLM_API_KEY 空占位） |
| `ci.yml` YAML 解析 + 6 job + 版本/缓存/timeout | ✅ 通过 |
| npm ci lockfile（feishu-bot/frontend） | ✅ 存在 |
| Dockerfile 静态指令（FROM/COPY/EXPOSE/HEALTHCHECK/CMD、说明注释） | ✅ 4/4 通过（未 build） |
| Mermaid 架构图 / CI 徽章 / compose 快速启动（README） | ✅ 存在 |

**未实测**：所有 `docker build`/`docker compose` 命令、GitHub Actions 实际运行（见 2.7、3.1）。

---

## 六、故障排查

| 现象 | 排查 |
|---|---|
| `docker compose up` 后 backend unhealthy | `docker compose logs backend`；容器内 DB 目录权限、依赖安装；`/api/health` 是否返回 |
| agent 起不来 / 502 | 先看 backend 是否 healthy；`AGENT_ENGINE`/`LLM_PROVIDER` 环境变量；`docker compose logs agent` |
| 前端页面空白/接口 404 | 检查 build args `VITE_API_BASE/VITE_AGENT_BASE` 是否为**浏览器可访问**地址；改后需 `--build` |
| mcp_bridge unhealthy | 容器内是否安装 Node/npm；`/health` 的 servers 连接状态；npx 首次拉包需要网络（或配置 npm 镜像） |
| 容器内路径追踪失败 | 预期：PT NC 不在容器内；改用本机直跑 backend 或见 2.6 |
| CI 徽章不显示 | 仓库需为 `zz-147/campus-twin` 且 workflow 至少跑过一次；路径大小写一致 |
| CI agent job 失败于 LLM 网络 | 该 job 已强制 `LLM_PROVIDER=mock`；检查测试是否被环境变量污染 |
| CI mcp job 安装失败 | mcp 1.30 要求 Python >=3.10；确认 job 的 python-version（3.12/3.13） |

---

## 七、后续改进

1. 有 Docker 环境后补跑 `docker compose up`，把 2.7 的未实测项逐条勾掉并补截图。
2. 为 backend/agent/frontend/mcp_bridge 增加镜像构建 CI job（当前 CI 只跑离线测试，避免
   Actions 上构建耗时/网络波动）。
3. 若要真正容器化 PT NC 数据链路，可在 Linux 宿主用 `network_mode: host` + 独立 compose
   profile 验证（Windows Docker Desktop 不支持）。
4. 可引入 `.env.docker.example` 与 `docker compose --profile` 拆分「仿真演示」与「真实 NC」两种模式。
