# Campus Twin MCP Server（W13-A）

把 campus-twin 的 **10 个只读工具 + 4 个资源 + 3 个标准提示词**协议化为标准 MCP Server，
可通过 **stdio** 接入 Claude Desktop、Cursor 或任意 MCP 客户端。数据全部来自正在运行的
campus-twin backend（默认 `http://127.0.0.1:8000`，netcon 驱动真实数据）。

```
Claude Desktop / Cursor / 任意 MCP 客户端
        │  stdio（JSON-RPC over stdin/stdout）
        ▼
mcp_server/server.py  ── FastMCP 1.30 ── 10 tools / 4 resources / 3 prompts
        │  httpx（同步、短连接）
        ▼
campus-twin backend :8000  ── REST /api/*（只读）──  NC → PT / SQLite
```

## 目录结构

```
mcp_server/
├─ server.py                     FastMCP stdio server（name=campus-twin, version=0.1.0）
├─ core.py                       工具/资源/提示词实现 + 后端 httpx 客户端（与 MCP SDK 解耦，便于 pytest）
├─ verify_client.py              真实验收：stdio 启动 server → list/read/call 全链路断言
├─ requirements.txt              mcp>=1.30,<2 + httpx
├─ config/
│  ├─ claude_desktop_config.json Claude Desktop 配置示例（绝对路径）
│  └─ cursor_mcp.json            Cursor 配置示例
├─ tests/test_mcp_server.py      30 条 pytest（mock httpx，离线）
└─ logs/                         运行日志（mcp_server.log / verify_client_*.log，自动创建）
```

## 快速开始

### 1) 启动后端（必须）

```powershell
cd D:\zdsh\campus-twin\backend
$env:DRIVER='netcon'
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 2) 验收 MCP Server（真实 stdio 协议）

```powershell
cd D:\zdsh\campus-twin\mcp_server
# 正常后端 + 后端不可用（127.0.0.1:9）双场景断言
& 'D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe' verify_client.py --bad-backend
# 期望最后输出：RESULT: ALL PASS（退出码 0）
```

`verify_client.py` 会：启动 `server.py` 子进程 → `initialize` 断言
`serverInfo.name=campus-twin / version=0.1.0` → `tools/list ≥ 10` →
`resources/list ≥ 4` → `prompts/list ≥ 3` → 实际调用 `get_devices` / `get_scene` →
`read_resource(campus://scene / campus://docs/network-design)` →
`get_prompt(troubleshoot_port_down)`；随后用 `BACKEND_BASE=http://127.0.0.1:9`
再启动一次，断言工具返回中文错误文本且 server 不崩。

### 3) 接入 Claude Desktop

把 `config/claude_desktop_config.json` 的 `mcpServers` 合并到：

- Windows：`%APPDATA%\Claude\claude_desktop_config.json`
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "campus-twin": {
      "command": "D:\\zdsh\\campus-twin\\.venv-mcp314\\Scripts\\python.exe",
      "args": ["D:\\zdsh\\campus-twin\\mcp_server\\server.py"],
      "env": { "BACKEND_BASE": "http://127.0.0.1:8000" }
    }
  }
}
```

### 4) 接入 Cursor

`config/cursor_mcp.json` 内容同上，合并到项目 `.cursor/mcp.json` 或全局
`~/.cursor/mcp.json`，然后在 Cursor Settings → MCP 中启用 `campus-twin`。

> 修改配置后需重启客户端；server 进程只监听 stdio，不占端口。
> 手动验证 stdio server 是否能启动（应无 stdout 输出、进程保持运行）：
> `D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe server.py`

## Tools（10 个，全部只读）

| 工具 | 参数 | 后端接口 | 说明 |
|---|---|---|---|
| `get_devices` | — | `GET /api/devices` | 设备清单（hostname/management_ip/reachability/source） |
| `get_topology` | — | `GET /api/topology` | 拓扑 nodes + links（含端口/速率） |
| `get_hosts` | — | `GET /api/hosts` | 终端主机清单与接入关系 |
| `get_issues` | — | `GET /api/issues` | 活跃健康问题（含 stale_source） |
| `get_alerts` | `state=active\|cleared\|info\|all` | `GET /api/alerts?state=&limit=50` | 告警列表（默认 active） |
| `get_alert_groups` | — | `GET /api/alerts/groups?state=active` | 根因分组 + 下游抑制 |
| `get_notify_status` | — | `GET /api/notify/status` | Webhook 通知状态 |
| `trace_path` | `source_ip, dest_ip` | `POST /api/path-trace` | 路径追踪（只读诊断；NC flow-analysis + 拓扑补全，超时 45s） |
| `get_scene` | — | `GET /api/scene` | 3D 场景配置（楼栋/设备/坐标/状态） |
| `get_audit` | `limit=20`（1-100） | `GET /api/audit?limit=` | 最近操作审计 |

返回值为**紧凑 JSON 文本**（`json.dumps(..., separators=(",",":"))`）；后端不可用/HTTP 错误/
非法 JSON 时返回 `错误：...` 文本，**不抛异常、server 不退出**。

## Resources（4 个，只读）

| URI | 来源 | MIME |
|---|---|---|
| `campus://scene` | `GET /api/scene` | application/json |
| `campus://topology` | `GET /api/topology` | application/json |
| `campus://docs/network-design` | `docs/校园网络拓扑设计.md`（白名单） | text/markdown |
| `campus://docs/troubleshooting` | `docs/故障注入实验记录.md`（白名单） | text/markdown |

docs 资源只允许读取上述两个固定文件；任何其它 URI/路径穿越（`../`、绝对路径）都返回
`错误：资源不在只读白名单内`。

## Prompts（3 个标准 SOP）

| 提示词 | 参数 | 用途 |
|---|---|---|
| `troubleshoot_port_down` | `device_name, interface_name="UNKNOWN"` | 端口 down 排障：设备/拓扑/hosts/告警/路径/审计逐步取证 |
| `troubleshoot_link_down` | `source_device, target_device` | 链路 down 排障：定位 link、根因分组、下游抑制、影响面 |
| `onboard_device` | `device_name, building="unknown", model="unknown"` | 新设备接入检查清单（VLAN/上联/纳管/场景确认） |

提示词内嵌可用工具名与只读边界，要求“先给结论再给证据、不得编造、不得声称已执行修复”。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BACKEND_BASE` | `http://127.0.0.1:8000` | campus-twin backend 地址 |
| `MCP_DOCS_DIR` | `campus-twin/docs` | docs 资源目录（仍受白名单文件名约束） |
| `MCP_LOG_FILE` | `mcp_server/logs/mcp_server.log` | 日志文件路径 |
| `MCP_LOG_LEVEL` | 未使用（INFO 固定） | 预留 |

## 安全边界

1. **只读**：10 个工具只调用后端 GET；唯一 POST 是 `/api/path-trace`（路径诊断，
   后端不写库）。不暴露告警删除/清空/静默、场景增删改、审计写等任何写接口。
2. **资源白名单**：docs 仅两个固定文件，拒绝路径穿越与任意文件读取。
3. **不越权访问数据源**：server 只访问 `BACKEND_BASE`，不直接读 SQLite / 不操作 PT。
4. **stdout 纯净**：stdio 协议下 stdout 只传 JSON-RPC；日志只写文件 + stderr。
5. **日志最小化**：只记录 tool/resource 名、ok、耗时、错误前 200 字符，不记录完整业务数据。

## 测试

```powershell
# 30 条 pytest（mock httpx，离线；不需要 mcp SDK，使用项目主 venv 3.9）
cd D:\zdsh\campus-twin\mcp_server
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pytest tests -q
# => 30 passed
```

覆盖：10 工具注册与接口映射、紧凑 JSON、告警 state/limit 校验与透传、审计 limit 钳制、
trace_path POST body、后端连接失败/HTTP 500/非法 JSON 的错误文本、默认 client 注入、
docs 白名单与路径穿越、资源读取、3 个 prompts 内容、源码只读边界（唯一 POST 为 trace_path）。

真实 MCP 协议验收见上文 `verify_client.py --bad-backend`。

## 调试

- **客户端里看不到 server**：先手动运行 `python server.py`（应静默挂起）；再检查
  `logs/mcp_server.log`、`logs/verify_client_*.log`（server stderr）。
- **工具返回“无法连接后端服务”**：确认 backend 8000 在跑、`BACKEND_BASE` 正确
  （PowerShell 检查：`curl.exe http://127.0.0.1:8000/api/health`）。
- **路径含空格/中文**：JSON 配置里用双反斜杠绝对路径；不要把 `server.py` 复制到别处。
- **MCP 版本**：本 server 针对 **mcp 1.30.x**（FastMCP）；mcp 2.x 将 FastMCP 改名为
  `MCPServer`，本目录未适配。

## 已知限制

1. 同步 httpx 每工具一次短连接，无连接池/缓存；单次调用毫秒级，当前场景足够。
2. `trace_path` 依赖 NC flow-analysis，最坏等待约 45s（后端超时 30s + 开销）；
   客户端需允许较长工具调用。
3. 无鉴权：MCP 接入层默认本机使用；如暴露给远端需在客户端/网关层加访问控制。
4. resources 文档为静态文件快照，docs 更新后客户端重新读取即可。
