# Packet Tracer 环境配置清单（Network Controller + Real World Access）

> 目标：让 Windows 宿主机上的 Python 程序能通过 `http://localhost:58000/api/v1`
> 读取 PT 仿真网络的真实运行状态（拓扑/设备/主机/健康问题/路径追踪）。

## 0. 版本要求

| 项 | 要求 | 本机实测 |
|---|---|---|
| Cisco Packet Tracer | **9.0.0 或以上**（需自带 Network Controller + Real World Access） | ✅ 9.0.0.810（64 位） |
| 安装路径 | `C:\Program Files\Cisco Packet Tracer 9.0.0\` | ✅ 有效 Cisco 数字签名 |
| Python | 3.9+ | ✅ 3.9.9 |
| 端口 | 1024–65535，本项目统一用 **58000** | 待配置 |

> ✅ 老的 `Cisco Packet Tracer 7.0` 已于 2026-10-29 卸载（安装包也已删除）。
> 本机现在只有 9.0.0 一个版本，不会开错。

## 1. 打开示例工程

`文件 → 打开`：

```
C:\Program Files\Cisco Packet Tracer 9.0.0\saves\02 Infrastructure Automation\Network Controller\
```

先开 `netcon_initial_setup.pkt` 完成控制器初始化，再开 `netcon.pkt`（示例网络）。
示例工程的默认管理员账号一般是 **admin / cisco**。

## 2. 打开外部访问开关（Preferences）

`Options → Preferences` → 勾选：

- [x] **Enable External Access for Network Controller REST API**

> 不勾这一项，NC 设备上的 Real World Access 配置面板是只读的，无法启用端口。

## 3. 在 Network Controller 设备上启用 HTTP 服务

在拓扑里点击 **Network Controller** 设备 → **Config** 标签 → **Real World Access**：

- [x] **Access Enabled**
- **HTTP Port**：`58000`（1024–65535 之间）
- **Server Status**：应显示 HTTP server 正在监听

> 安全限制：PT 只允许**本机 localhost** 访问该端口，远程连接会被拒绝。这对我们正好。

## 4. 验证（Windows 宿主机）

PowerShell 里直接跑：

```powershell
curl.exe -X POST http://localhost:58000/api/v1/ticket `
  -H "content-type: application/json" `
  --data '{\"username\":\"admin\",\"password\":\"cisco\"}'
```

预期返回（关键字段 `serviceTicket`）：

```json
{
  "response": {
    "idleTimeout": 900,
    "serviceTicket": "NC-34-de50f79c8dab4bb4b478-nbi",
    "sessionTimeout": 3600
  },
  "version": "1.0"
}
```

更完整的验证（推荐）：

```powershell
python D:\zdsh\campus-twin\tools\netcon_probe.py --port 58000
```

脚本会自动登录、逐个探测端点，并把原始 JSON 存到 `D:\zdsh\campus-twin\data\raw\`。

## 5. 常用 API 端点（已从 PT 9.0 自带文档确认）

| 端点 | 用途 |
|---|---|
| `POST /api/v1/ticket` | 登录换 token（后续请求头 `x-auth-token`） |
| `GET /api/v1/network-device` | 设备清单（型号/IP/角色/软件版本） |
| `GET /api/v1/network-device/count` | 设备数量 |
| `GET /api/v1/network-device/{id}` | 单设备详情 |
| `GET /api/v1/topology/physical-topology` | 物理拓扑（nodes + links） |
| `GET /api/v1/host` | 主机/终端清单 |
| `GET /api/v1/discovery` | 设备发现任务 |
| `GET /api/v1/global-credential/cli` | CLI 凭据 |
| `GET /api/v1/assurance/network-health` | 网络健康总览 |
| `GET /api/v1/assurance/health-issues` | 健康问题/告警（**这是故障数据源**） |
| `POST /api/v1/flow-analysis` | 路径追踪（故障定位） |
| `GET /api/v1/flow-analysis/{id}` | 路径追踪结果 |

## 6. 故障注入（演示闭环用）

在 PT 里手工制造故障，然后观察我们系统的告警是否联动：

| 故障场景 | 操作 | 预期 |
|---|---|---|
| 端口宕机 | 接入交换机连接 PC 的端口 `shutdown` | NC 健康问题出现，系统告警 |
| 链路中断 | 删除两台交换机之间的链路 | 拓扑变化 + 设备不可达 |
| 路由缺失 | 核心路由器删除 `network` 宣告 | 跨网段不通，路径追踪发现断点 |
| VLAN 错误 | 把接入端口划错 VLAN | 主机掉线 |
| 环路 | 在两台交换机之间加两条链路（不启 STP 保护） | 广播风暴/健康问题 |

> 故障注入后等 30–60 秒让 NC 的 discovery/assurance 周期跑一遍，再触发我们系统的采集。
