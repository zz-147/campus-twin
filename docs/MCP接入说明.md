# MCP 接入说明（W13：MCP Server + MCP Client 桥接）

> 目标：把平台能力以 MCP 协议对外开放，同时让 Agent 通过 MCP 使用外部工具。
> 环境：MCP SDK 要求 Python ≥3.10；项目主环境 3.9，因此 MCP 相关进程使用独立
> `D:\zdsh\campus-twin\.venv-mcp314`（Python 3.14 + `mcp 1.30.0`），主项目零影响。

---

## 一、架构总览

```
【A. MCP Server（对外）】
  Claude Desktop / Cursor / VS Code / MCP Inspector
        │  stdio（MCP 协议）
        ▼
  mcp_server/server.py（FastMCP，10 tools / 4 resources / 3 prompts）
        │  HTTP
        ▼
  campus-twin 后端 :8000（真实网络数据）

【B. MCP Client 桥接（对内）】
  Agent :8100（py3.9，LangGraph）
        │  HTTP（127.0.0.1:8200）
        ▼
  mcp_bridge/bridge.py（py3.14，mcp ClientSession/stdio）
        │  stdio（MCP 协议）
        ├── filesystem MCP Server（只读 configs/、docs/）
        └── memory MCP Server（本地记忆）
```

## 二、A：自建 MCP Server（`campus-twin/mcp_server/`）

### 能力清单
| 类型 | 内容 |
|---|---|
| **Tools（10，全部只读）** | get_devices / get_topology / get_hosts / get_issues / get_alerts / get_alert_groups / get_notify_status / trace_path / get_scene / get_audit |
| **Resources（4）** | `campus://scene`、`campus://topology`、`campus://docs/network-design`、`campus://docs/troubleshooting`（后两个为只读白名单文件） |
| **Prompts（3）** | troubleshoot_port_down / troubleshoot_link_down / onboard_device |

### 接入方式
```powershell
# 方式 1：手工启动（stdio，静默挂起）
D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe D:\zdsh\campus-twin\mcp_server\server.py

# 方式 2：验收（真实 stdio 客户端全链路）
cd D:\zdsh\campus-twin\mcp_server
& 'D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe' verify_client.py --bad-backend
```
客户端配置见 `mcp_server/config/claude_desktop_config.json` 与 `cursor_mcp.json`（绝对路径 stdio）。

### 可视化验证（无需安装客户端）
```powershell
npx -y @modelcontextprotocol/inspector -- <py314 绝对路径> <server.py 绝对路径>
# 打开输出里的 http://127.0.0.1:6274?MCP_INSPECTOR_API_TOKEN=...
```
Inspector 里点 Connect → 可见 10 tools / 4 resources / 3 prompts，点工具可 Run。

## 三、B：MCP Client 桥接 + Agent 集成（`campus-twin/mcp_bridge/` + `agent/`）

### 启动
```powershell
cd D:\zdsh\campus-twin\mcp_bridge
D:\zdsh\campus-twin\.venv-mcp314\Scripts\python.exe bridge.py --port 8200
```
- `GET /health`：两个 server 连接状态与工具数
- `GET /tools`：全部外部工具（23 个）
- `POST /call`：`{server, tool, arguments}` → 统一返回

### 外部 MCP Servers
| server | 工具数 | 作用 |
|---|---|---|
| filesystem | 14 | 只读访问 `configs/`、`docs/`（白名单目录，写/越权被拒） |
| memory | 9 | 本地记忆（设备别名/值班记录） |

### Agent 侧新增（6 个工具，总数 23）
`mcp_list_tools` / `mcp_list_dir` / `mcp_read_file` / `mcp_search_files` / `mcp_memory_search` / `mcp_memory_add`（仅本地记忆写入，标注非网络设备写操作）
- 5 个只读 MCP 工具加入诊断（8+5=13）与 onboarding 白名单
- 确定性路由新增 `mcp_read`（路径+读/VLAN 等）与 `mcp_tools`（MCP 工具清单）
- `trace.mcp_calls` 记录 server/tool/ok/elapsed_ms
- Bridge 不可用：工具 `ok=false` + 中文提示，不崩溃不编造

## 四、实测记录（真实环境）

| 验证项 | 结果 |
|---|---|
| MCP Server `verify_client.py` | **ALL PASS**：tools=10 / resources=4 / prompts=3；get_devices 返回 5 台真实设备；读 scene 3713 字符、设计文档 8409 字符；后端不可用降级 PASS |
| MCP Server pytest | **30 passed** |
| Bridge `verify_bridge.py` | **ALL_BRIDGE_VERIFY_PASS**：total_tools=23（filesystem 14 + memory 9）；读 `configs/core-3650.txt` PASS；memory 写/查 PASS；越权写被拒 |
| Bridge unittest | **10 passed** |
| Agent pytest | **256 passed** |
| Agent live（真实 DeepSeek + Bridge） | 「读 configs/core-3650.txt 的 VLAN 配置」→ `route_source=rule`、`plan=[mcp_read]`、`mcp_calls=[filesystem/read_text_file ok 6.88ms]`、回答含真实 VLAN 10/20/... 与 trunk 要点；「MCP 接入了哪些工具」→ 列出 filesystem 14 + memory 9 = 23 |
| 降级 | Bridge 未启动时 Agent 返回"请先启动 mcp_bridge/bridge.py"，不崩 |
| 安全 | 只读白名单、路径穿越拒绝、只监听 127.0.0.1、全部调用写 `logs/mcp_bridge.log` |

## 五、演示脚本（约 1 分钟）

1. 打开 MCP Inspector（URL 见终端输出）→ Connect → 展示 10 个工具 → Run `get_devices` 看真实 5 台设备
2. 在 3D 大屏 AI 面板问：「帮我读一下 configs/core-3650.txt 里 VLAN 配置」→ 看它走 MCP 读文件并回答真实 VLAN
3. 问：「MCP 现在接入了哪些工具？」→ 23 个外部工具清单
4. （可选）Claude Desktop / Cursor 里直接连 MCP Server 演示

## 六、面试讲点 / 简历写法

> **MCP 双向集成**：①把平台 10 个运维工具、4 个资源、3 个提示词封装为 **MCP Server**（stdio，FastMCP），Claude Desktop / Cursor / VS Code 等任意 MCP 客户端可接入；②以 **MCP Client 桥接**（独立 Python 3.14 进程 + HTTP 适配层）为 Agent 接入 filesystem/memory 两个外部 MCP Server（23 个工具），实现工具协议化、只读权限隔离（白名单/路径穿越防护）与调用审计（trace.mcp_calls）。

**关键设计取舍**（面试可能问）：
- 为什么 MCP 进程单独用 Python 3.14？→ 官方 SDK 要求 ≥3.10，主服务 3.9 不动，用子进程/桥接解耦，零侵入
- 为什么做 Bridge 而不是 Agent 直接连？→ Agent 主环境 3.9 无法直接用 MCP SDK；Bridge 同时提供 SSH/权限/审计收口
- 安全：只读工具才对外；写能力不暴露；目录白名单 + localhost + 审计日志
