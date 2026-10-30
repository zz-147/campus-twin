# Campus Twin 后端（FastAPI 采集 + 检测服务）

校园网数字孪生运维平台的**数据接入 + 归一化 + 检测引擎 + 实时推送**服务。

数据源是 **Packet Tracer 9.x Network Controller（NC）的 Real World Access REST API**
（Cisco DNA Center 风格，`http://127.0.0.1:58000/api/v1`，只监听 localhost）。
PT 不暴露接口流量计数器，因此**端口流量/利用率等指标由确定性行为仿真引擎补齐**，
统一标注 `source="simulator"`，绝不冒充真实采集；设备/拓扑/主机/健康问题等
来自 NC 的数据标注 `source="netcon"`；W10 SNMP 真实设备数据标注 `source="snmp"`。

```
              ┌──────────────────────── app/collectors ────────────────────────┐
 NC REST ───▶ │ NetconDriver：ticket 认证/续期、并发采集、no-cache、退避降级     │
 （真数据）    │ SimulatorDriver：分时段负载 + 故障后流量重分布（确定性行为模型）  │
              └────────────────────────────┬───────────────────────────────────┘
                                           ▼ CollectResult（统一模型，带 source/observed_at）
              ┌──────────────────── app/detection（W3）────────────────────────┐
              │ DetectionEngine：host_down / device_unreachable / link_down /   │
              │ health_issue …；恢复判定优先用 host/device 实际状态（不依赖 NC   │
              │ issue 清除），issue 滞后标 stale_source                         │
              └────────────────────────────┬───────────────────────────────────┘
                                           ▼ 告警生命周期（active/cleared/info）
              ┌──────────────────────── app/pipeline ──────────────────────────┐
              │ 5s 采集循环 → 检测 → 告警 incident 去重/聚合 → SQLite WAL → WS  │
              └────────────────────────────┬───────────────────────────────────┘
                                           ▼
              FastAPI: /api/health /api/devices /api/topology /api/hosts
                       /api/issues /api/alerts /api/metrics/{device_id}
                       /api/network-health /api/detection/rules /ws
```

## 目录结构

```
backend/
├─ app/
│  ├─ config.py            环境变量配置（NC 地址/轮询间隔/DB 路径/驱动选择/告警 TTL）
│  ├─ models.py            统一 Pydantic 模型 + from_netcon 防御式归一化 + 时间基准字段
│  ├─ collectors/
│  │  ├─ base.py           Driver / MetricGenerator 协议 + CollectResult
│  │  ├─ netcon.py         httpx 异步客户端：ticket 认证续期、并发采集、no-cache、退避降级
│  │  ├─ simulator.py      行为仿真驱动（指标 source=simulator）
│  │  └─ snmp.py           W10 SNMP 真实设备驱动（MIB-II，source=snmp）
│  ├─ detection/
│  │  ├─ __init__.py
│  │  └─ rules.py          W3 自建检测规则 + 恢复判定（stale_source）+ 规则目录
│  ├─ store.py             SQLite WAL：快照/健康问题/告警生命周期/指标点 + TTL + scene/audit
│  ├─ scene.py             W9 场景配置服务（4 楼栋/12 设备 seed、CRUD、新设备检测/归属推断）
│  ├─ schemas.py           管理类 API 请求模型（场景/告警清空/路径追踪）
│  ├─ path_trace.py        W10 路径追踪（NC flow-analysis + 拓扑推断补全 + 2 分钟缓存）
│  ├─ alert_groups.py      W10 告警根因分组 + 下游抑制（展示层）
│  ├─ notify.py            W10 Webhook 通知（队列/重试/去重/状态）
│  ├─ pipeline.py          采集循环 + 检测引擎执行 + 告警去重/恢复聚合 + WS 广播
│  └─ main.py              FastAPI 应用（REST + WebSocket + lifespan）
├─ tests/                  pytest 单测（全部离线，不依赖 PT 在线）
│  ├─ fixtures/            固定 JSON 样本（空响应 / 真实响应形态 / 故障&恢复回放）
│  ├─ helpers.py           测试工厂（CollectResult/Device/Host/Issue/ScriptedDriver）
│  ├─ test_netcon_normalize.py
│  ├─ test_simulator.py
│  ├─ test_pipeline.py
│  ├─ test_detection_rules.py
│  ├─ test_host_freshness.py
│  ├─ test_alert_noise.py
│  ├─ test_stale_source_replay.py
│  ├─ test_store.py
│  ├─ test_alerts_manage.py
│  ├─ test_scene_api.py
│  ├─ test_audit.py
│  ├─ test_path_trace.py
│  ├─ test_alert_groups.py
│  ├─ test_notify.py
│  ├─ test_snmp_driver.py
│  └─ test_api.py
├─ requirements.txt
└─ README.md
```

## 快速开始

```powershell
# 1) 装依赖（官方 PyPI / 清华源不可用，用阿里云镜像；解释器用项目 venv）
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pip install --only-binary :all: `
  -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com `
  -r D:\zdsh\campus-twin\backend\requirements.txt

# 2) 启动服务（必须在 backend 目录下，app 是顶层包）
cd D:\zdsh\campus-twin\backend
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3) 验证
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/hosts
curl.exe http://127.0.0.1:8000/api/detection/rules
```

打开 <http://127.0.0.1:8000/docs> 可看 Swagger UI。

> 端口提示：如果 PT 里 Network Controller 的 **HTTP Port 不是 58000**（联调时曾实测 58001），
> 用环境变量覆盖即可：`$env:NC_BASE_URL='http://127.0.0.1:58001/api/v1'`。

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `NC_BASE_URL` | `http://127.0.0.1:58000/api/v1` | NC Real World Access 地址 |
| `NC_USERNAME` / `NC_PASSWORD` | `admin` / `cisco` | NC 管理员账号（PT 示例工程默认） |
| `NC_TIMEOUT` | `5.0` | 单次 HTTP 超时（秒） |
| `NC_RETRY_INTERVAL` | `15.0` | 连接失败后的首次重试间隔，之后指数退避 |
| `NC_MAX_RETRY_INTERVAL` | `120.0` | 退避上限（秒） |
| `POLL_INTERVAL` | `5.0` | 采集轮询周期（秒） |
| `COLLECT_TIMEOUT` | `30.0` | 单轮采集硬超时（防驱动卡死） |
| `DRIVER` | `netcon` | `netcon` / `simulator` / `auto` |
| `SIMULATE_METRICS` | `1` | 是否生成仿真指标（PT 无计数器，默认开） |
| `DB_PATH` | `backend/data/campus_twin.db` | SQLite 路径 |
| `METRICS_RETENTION_SECONDS` | `3600` | 指标点保留时长，超期自动清理 |
| `ALERT_RETENTION_DAYS` | `7` | 历史告警 TTL（只清理 cleared/info，active 不删） |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `MAX_ALERTS` | `200` | `/api/alerts` 默认上限 |
| `PATH_TRACE_CACHE_SECONDS` | `120` | W10 路径追踪同源/目的缓存时长 |
| `PATH_TRACE_TIMEOUT` | `30` | W10 NC flow-analysis 轮询总超时（秒） |
| `NOTIFY_ENABLED` | `false` | W10 Webhook 通知开关（还需配置 URL） |
| `NOTIFY_WEBHOOK_URL` | 空 | Webhook 接收地址（本地测试如 `http://127.0.0.1:9000/webhook`） |
| `NOTIFY_WEBHOOK_TYPE` | `generic` | `generic` / `dingtalk` / `feishu` / `wecom` |
| `NOTIFY_MIN_SEVERITY` | `critical` | 触发通知的最低级别（critical/warning） |
| `NOTIFY_DEDUP_SECONDS` | `300` | 同 `dedup_key` 去重窗口（秒） |
| `SNMP_DEVICES` | `[]` | W10 SNMP 设备 JSON 数组（name/ip/community/version/building） |
| `SNMP_TIMEOUT` | `2.0` | SNMP 单设备超时（秒） |
| `SNMP_RETRIES` | `1` | SNMP 重试次数 |

```powershell
# 示例：无 PT 时用纯仿真数据开发/演示
$env:DRIVER = 'simulator'
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m uvicorn app.main:app --port 8000
```

## 驱动切换说明

| `DRIVER` | 设备/拓扑/主机/健康问题 | 指标 |
|---|---|---|
| `netcon`（默认） | **真实 REST 采集**（`source="netcon"`）；NC 不可用时降级为空数据集继续运行 | 仿真（`source="simulator"`） |
| `simulator` | 行为模型生成的仿真校园网（`source="simulator"`） | 仿真（`source="simulator"`） |
| `auto` | 启动时探测 NC：可用用 netcon，不可用自动回退 simulator 并打 warning 日志 | 同左 |
| `snmp` | W10：SNMP v1/v2c 真实设备（MIB-II，`source="snmp"`）；无设备/全部失败时优雅降级 | **真实采集**（`source="snmp"`，带 `speed_bps/clamped`，`bps<=speed_bps`） |

- `SIMULATE_METRICS=0` 可关闭指标生成（`/api/metrics/*` 返回空，`/api/health.metrics_source=null`）。
- 驱动可插拔：实现 `app/collectors/base.py` 的 `Driver` 协议（`name/probe/collect/close`）
  即可接入新数据源（如 Replay 回放、Config 解析），pipeline/REST/WS 无需改动。
- 仿真故障注入（仅 `DRIVER=simulator` 或启用仿真指标时可用，用于演示“故障 → 告警 →
  流量重分布”闭环）：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/debug/simulator/link-failure `
  -H "content-type: application/json" --data '{\"link_id\":\"sim-l-core\"}'
curl.exe -X POST http://127.0.0.1:8000/api/debug/simulator/device-failure `
  -H "content-type: application/json" --data '{\"device_id\":\"sim-acc-b1\"}'
curl.exe -X POST http://127.0.0.1:8000/api/debug/simulator/reset
```

## W3 检测引擎（`app/detection/rules.py`）

### 为什么自建规则

2026-10-29 故障注入实验（`docs/故障注入实验记录.md`）显示：故障端 NC 30–60s 内报 issue、
后端 5s 内产生 critical 告警 ✅；但**恢复端 NC 的 `GET /assurance/health-issues` 清除延迟
超过 3 分钟**（host ping 早已 SUCCESS、健康度回 100%，issue 仍在）❌。
因此恢复判定不能只依赖 NC issue 生命周期，必须直接看 **host / device 的实际状态**。

### 规则清单（`GET /api/detection/rules` 可在线查看）

| 规则 id | 数据源 | 触发条件 | 级别 | 恢复判定 |
|---|---|---|---|---|
| `host_down` | NC `GET /host` → `pingStatus` | `FAILURE` | critical | `SUCCESS`（不依赖 NC issue 清除） |
| `device_unreachable` | NC `/network-device` → `reachabilityStatus` | `Unreachable` | critical | 恢复可达 |
| `device_credential` | `collectionStatus` | `Wrong credential` / `Unreachable` | warning | 采集状态恢复 |
| `link_down` | `/topology/physical-topology` → `linkStatus` | `down` 或链路从拓扑消失 | warning | 恢复 `up` / 链路重新出现 |
| `device_offline` | 设备列表变化 | 上轮存在的设备本轮消失（端点未报错） | critical | 设备重新出现 |
| `health_issue` | `/assurance/health-issues` | 新 NC 原生 issue | critical/warning | ① issue 消失；② **issue 仍在但 host/device 已恢复 → `stale_source=True` 的 cleared** |
| `device_online` / `link_up` | 设备/拓扑列表 | 新增设备/链路 | info | 一次性事件（`state=info`） |

同一 `dedup_key` 同时最多一条 `active`；故障持续期间不重复入库/推送。

### 恢复判定：数据源优先（stale_source）

`health_issue` 恢复时会交叉校验 issue 指向的 host/device 实际状态（按 `issueSource`、
`issueName`、`issueDescription` 中的名字/IP 匹配）：
- host `pingStatus=SUCCESS` 或 device `reachable` → 即使 NC issue 仍在，也生成 cleared；
- 该 cleared 带 `stale_source=true`，detail/clear_reason 注明
  **“NC 问题记录滞后，数据源已恢复”**，`/api/issues` 中对应 issue 也会标 `stale_source=true`；
- 已 stale 恢复的 issue 若 NC 记录仍持续返回，会被**抑制不再重复告警**（防止 flap），
  直到 NC issue 消失或目标 host/device 再次故障。

### 告警降噪（对应 11 条集中清除噪声）

1. **incident 生命周期**：每条告警 = 一次事件生命周期；恢复只把原行 `state` 置为
   `cleared`，不再逐条新增 info “已清除”行；
2. **恢复聚合**：同一轮多条恢复在 WebSocket 上合并为**一条** `alerts_cleared` 摘要
   （例如“本轮清除 6 条告警（其中 3 条因 NC 问题记录滞后…）”）；
3. **历史告警 TTL**：默认保留 7 天（`ALERT_RETENTION_DAYS`），后台定期清理
   `cleared`/`info`，`active` 故障不因时间长被删除；
4. **API 过滤**：`/api/alerts?state=active|cleared|info|all`，配合 `summary` 统计。

### 时间基准（observed_at vs source_ts）

PT 仿真设备时钟与宿主机实测相差约 10 分钟（NC `issueTimestamp=21:33:14` vs 宿主机
`21:43:42`），两者不能混用：

- `Alert.observed_at` / `HealthIssue.observed_at` = **宿主机采集时间**（展示/排序/告警用）；
- `source_ts` = NC 原始时间字符串（审计/演示时钟偏差用）；
- `created_at` 保留为 `observed_at` 的兼容别名（前端旧字段）。

### host 新鲜度修复（W3-A）

故障期间后端 `/api/hosts` 曾显示 `SUCCESS`（NC 已 `FAILURE`），修复方式：

1. netcon 驱动**每轮**都重新 `GET /host` 并用当轮 `seen_at=now` 重建 host 模型（不做增量
   合并/缓存）；
2. 所有 GET 请求带 `Cache-Control: no-cache, no-store` / `Pragma: no-cache`，并附加
   `_ts=<epoch_ms>` cache-buster，避免内嵌 HTTP 服务缓存旧响应；
3. `/api/hosts` 返回 `observed_at` / `snapshot_age_seconds`，健康接口暴露
   `driver_status.host_fetch_count`，可据此确认主机状态是“每轮新鲜的”。

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务健康 + 采集状态 + 检测引擎/告警生命周期统计（`detection`）。**NC 离线时仍返回 `status="ok"`** |
| GET | `/api/devices` | 设备快照（0 设备时 `count=0, devices=[]`） |
| GET | `/api/topology` | 拓扑 nodes + links |
| GET | `/api/hosts` | 主机清单；含 `ping_status` / `last_seen` / `observed_at` / `snapshot_age_seconds` |
| GET | `/api/issues` | 当前活跃健康问题；每条含 `observed_at` / `source_ts` / `stale_source` |
| GET | `/api/alerts?limit=100&severity=&state=all` | 告警历史：`state=active/cleared/info/all`，响应含 `summary` 统计 |
| GET | `/api/alerts/summary` | 告警生命周期统计（active/cleared/info/total） |
| GET | `/api/metrics/{device_id}?limit=240&metric=` | 设备指标时序（`source` 恒为 `simulator`）；端口指标带 `speed_bps` / `clamped`（W3.1 物理约束） |
| GET | `/api/network-health` | 网络健康总览（W3.1 字段自洽：`device_*`/`health_percentage` 自算 + `nc_*` 原始字段并存 + `source_of_truth`/`device_count` 审计字段） |
| GET | `/api/drivers` | 当前驱动 / 指标生成器 / 可选驱动说明 |
| GET | `/api/detection/rules` | **W3 检测规则目录 + 告警生命周期统计 + 判定说明** |
| DELETE | `/api/alerts/{id}` | **W9 软删除已恢复告警**（`deleted_at`；active 返回 409 只能 silence） |
| POST | `/api/alerts/clear` | **W9 批量软删**：`{"scope":"cleared"\|"all_cleared","before":epoch?}` → `{"deleted":N}` |
| POST | `/api/alerts/{id}/silence` | **W9 静默活跃告警**（`status=silenced`，不删除、不改 state） |
| POST | `/api/alerts/{id}/restore` | **W9 撤销软删除** |
| GET | `/api/audit?limit=&action=&operator=` | **W9 操作审计**（operator/action/target/detail/result/时间；`/api/alerts/audit` 为别名） |
| GET | `/api/scene` | **W9 场景配置**：`{version, theme, buildings:[{id,name,position,size,style,devices:[{name,type,position,status,source,...}]}]}` |
| GET | `/api/scene/pending` | **W9 待确认新设备**：inventory 中未纳入 scene 的设备 + 归属推断 |
| POST | `/api/scene/buildings` | **W9 新增楼栋**（默认 `status=pending`） |
| POST | `/api/scene/buildings/{id}/devices` | **W9 设备 upsert**（同楼坐标冲突自动偏移并提示） |
| POST | `/api/scene/devices/{name}/confirm` | **W9 确认 pending 设备**（→ confirmed） |
| DELETE | `/api/scene/devices/{name}` | **W9 删除场景设备**（版本化 tombstone + 审计） |
| POST | `/api/scene/reset` | **W9 重置默认 4 楼栋 / 12 设备** |
| POST | `/api/path-trace` | **W10.1 路径追踪**：`{source_ip,dest_ip}` → `{ok,trace_status,hops[],links[],synthetic_hop_count,elapsed_ms,error}`（NC flow-analysis + 拓扑补全，2 分钟缓存） |
| GET | `/api/alerts/groups?state=active` | **W10 告警根因分组**：`{count,groups:[{group_id,root_cause,root_device_*,severity,title,member_count,suppressed_count,members[]}]}` |
| POST | `/api/alerts/groups/{group_id}/ack` | **W10 整组确认**（写 audit） |
| GET | `/api/notify/status` | **W10 Webhook 通知状态**（enabled/type/min_severity/sent_count/last_error…） |
| POST | `/api/notify/test` | **W10 发送测试通知**（返回 `{ok,sent,error,status}`） |
| WS | `/ws` | 实时快照 + 增量告警 + 恢复摘要 |

## W9 数据管理 + 场景配置 + 新设备检测（2026-10-30）

设计契约：`docs/W9-Agent扩展设计.md`。后端提供前端（按钮操作）与 Agent（action 执行）
共用的写接口，**所有写操作都要求 `X-Operator` 请求头并写审计**；不依赖 Agent 也能用。

### 1) 告警数据管理（软删 / 清空 / 静默 / 撤销 + 审计）

| 操作 | 接口 | 规则 |
|---|---|---|
| 单条删除 | `DELETE /api/alerts/{id}` | **active 告警禁止删除**（409，只能 silence）；cleared/info 软删除（`deleted_at`） |
| 批量清空 | `POST /api/alerts/clear` | `scope=cleared` 只删已恢复；`all_cleared` 连同 info 一次性事件；`before` 限制时间；active 永不删除 |
| 静默 | `POST /api/alerts/{id}/silence` | 只对 active 有效，`status=silenced`，不删除、不改变 `state` |
| 撤销 | `POST /api/alerts/{id}/restore` | `deleted_at=NULL` 恢复显示 |
| 查询 | `GET /api/alerts?include_deleted=true&status=open\|silenced` | 默认过滤软删除；`include_deleted` 供审计/内部使用 |
| 审计 | `GET /api/audit?action=alert.delete` | `result=ok` 或 `rejected:<原因>` 均记录 |

不变式：**active 告警不可删除（只能静默）**，硬删除仅由历史 TTL 后台清理执行；
软删除可撤销，所有写操作（包含被拒绝的尝试）都会写 `audit_log`。

```powershell
# 清空已恢复告警（前端按钮 / Agent action 最终都调它）
curl.exe -X POST http://127.0.0.1:8000/api/alerts/clear `
  -H "content-type: application/json" -H "X-Operator: agent" `
  --data '{\"scope\":\"cleared\"}'
# 静默一条活跃告警
curl.exe -X POST http://127.0.0.1:8000/api/alerts/15/silence -H "X-Operator: web-ui"
# 查看审计
curl.exe "http://127.0.0.1:8000/api/audit?limit=20"
```

### 2) 场景配置（配置驱动，替代前端硬编码 layout）

- 存储：SQLite `scene_config` 单行 JSON 文档，**每次写操作 `version++`**；
  首次访问自动 seed **4 楼栋 / 12 设备**（来源 `docs/校园网络拓扑设计.md` §九/§十）：
  - 中心机房 `core` (0, 0)：Core-3650、Router-4331、Server-1、NC
  - 教学楼 `teach` (-120, 40)：SW-JX-2960、PC-JX1、PC-JX2
  - 实训楼 `train` (120, 40)：SW-SX-2960、PC-SX1
  - 宿舍楼 `dorm` (0, -140)：SW-SS-2960、PC-SS1、PC-SS2
- 设备坐标：`position={x,y,z}` 为**世界坐标**（与前端低模布局一致，y=楼顶+3.8），
  可直接用于 Three.js 渲染；`status=pending|confirmed`，`source=seed|manual|agent`。
- 写接口（`POST /api/scene/buildings`、`.../devices`、confirm、delete、reset）：
  - 需 `X-Operator`（默认 `web-ui`；Agent/前端代理传 `agent`）→ 写 `audit_log`；
  - 设备 upsert **同楼坐标碰撞检测**：与已有设备间距 < 8 单位时自动沿 x 偏移，
    响应 `{"adjusted": true, "message": "坐标与已有设备冲突，已自动偏移..."}`；
  - `DELETE` 为版本化软删除（设备从 buildings 移除 + `removed_devices` tombstone）；
  - `POST /api/scene/reset` 恢复默认 4 楼栋/12 设备。

```jsonc
// GET /api/scene（节选）
{"version": 3,
 "theme": {"name": "low-poly-campus", "style": "low-poly", "accent": "#38bdf8"},
 "buildings": [{"id": "core", "name": "中心机房",
   "position": {"x": 0.0, "z": 0.0},
   "size": {"width": 48.0, "depth": 36.0, "height": 16.0},
   "style": {"color": "#2f6f9e", "opacity": 0.35, "borderColor": "#7dd3fc"},
   "devices": [{"name": "Core-3650", "type": "switch",
                "position": {"x": -18.0, "y": 19.8, "z": 0.0},
                "status": "confirmed", "source": "seed",
                "model": "Catalyst 3650", "mgmt_ip": "192.168.10.1"}]}]}
```

### 3) 新设备检测（供 Agent 侦察 / 前端提示）

- pipeline 每轮采集后比对 `inventory（NC /network-device）` 与 `scene_config`，
  `GET /api/scene/pending` 返回未纳管设备：`mgmt_ip` / `platform` / `reachability` /
  `collection_status` / `connected_to`（拓扑邻居 + 端口）/ `inferred_building`。
- **归属推断（五级规则的前三级，后端先给出建议）**：
  1. `topology_neighbor`：新设备连到的已纳管设备所在楼栋（confidence 0.9）；
  2. `hostname_keyword`：JX/教学→教学楼、SX/实训→实训楼、SS/宿舍→宿舍楼、
     CORE/核心/机房→中心机房（0.8）；
  3. `ip_prefix`：`192.168.20.→teach`、`.30.→train`、`.40.→dorm`、
     `.10./.50.→core`（0.6，可在 scene 文档 `device_assignment` 中配置）。
  4. 推断不出返回 `inferred_building: null`，由 Agent/用户确认后通过
     `POST /api/scene/buildings/{id}/devices` 落位，再 `confirm`；**不自动建楼**。
- 新设备默认 `status=pending`（前端浅色/虚线 + 「新设备」角标），确认后转 `confirmed`。

### 4) 审计与操作人

`audit_log(operator, action, target, detail, result, created_at)`；action 示例：
`alert.delete` / `alert.clear` / `alert.silence` / `alert.restore` /
`scene.building.add` / `scene.device.upsert` / `scene.device.confirm` /
`scene.device.delete` / `scene.reset`。被拒绝的写操作（如删除 active 告警）也会记录
`result=rejected`，便于审计追踪。

## W10 路径追踪 + 告警根因分组 + Webhook + SNMP（2026-10-30）

设计契约：`docs/W10-路径追踪告警聚合设计.md`。

### 1) 路径追踪：`POST /api/path-trace`

- 调用 NC `POST /api/v1/flow-analysis {"sourceIP","destIP"}` 拿 `flowAnalysisId`，
  再轮询 `GET /api/v1/flow-analysis/{id}` 直到 `request.status=COMPLETED` 或
  `path_trace_timeout`（默认 30s）超时；
- 归一化 `networkElementsInfo` → `hops[]`（device_name/device_id/ip/ingress_port/
  egress_port/role=source|transit|destination）与 `links[]`（相邻 hop，优先匹配真实拓扑
  链路，匹配不到给 `synthetic=true` 的合成链路，前端按 hop 顺序连线）；
- **W10.1 拓扑推断补全**：PT 的 flow-analysis 常漏中间设备（实测漏接入交换机 SW-JX-2960）。
  归一化后用当前快照构图补全：
  1. 图节点 = 设备（`/api/devices`）+ 主机（`/api/hosts`）；边 = topology links
     （带两端端口 `start_port_name/end_port_name`）+ hosts 接入关系
     （`connected_device_id` 或 `connected_device_name` + `connected_interface`）；
  2. 对相邻 NC hops (A→B)：若 A、B 无直接边，做 BFS 最短路，把路径上的中间设备按顺序
     插入 hops（`role=transit`，ingress/egress 端口能拿到就填，否则 `UNKNOWN`）；
  3. 补全 hop 标 `synthetic=true, inferred=true, source="topology_inference"`，links 同步
     补齐并带 `reason=topology|host_attachment|hop_sequence`；`warnings` 追加
     “中间设备 X 由拓扑推断补全（NC 未返回）”；
  4. 图上找不到路径时保持 NC 原始 hop，并 warning“拓扑不可达”；
- 同 (source,dest) 结果缓存 **2 分钟**；并发同请求只打一次 NC；失败返回
  `ok=false + 中文 error`，不阻塞采集循环。

**PT 9.0 实测结构**（2026-10-30，`192.168.20.10 → 192.168.50.10`，1.0s 完成）：
```jsonc
// POST → {"response":{"flowAnalysisId":"FOGN...","url":"/flow-analysis/FOGN..."},"version":"1"}
// GET  → {"response":{"request":{"status":"COMPLETED","sourceIP":...,"destIP":...},
//          "networkElementsInfo":[
//            {"name":"PC-JX1","id":"PTT...","ip":"192.168.20.10","type":"Pc",
//             "ingressInterface":null,"egressInterface":null},
//            {"name":"Core-3650","id":"CAT...","ip":"192.168.20.1",
//             "ingressInterface":{"physicalInterface":{"name":"Vlan20"}},
//             "egressInterface":{"physicalInterface":{"name":"GigabitEthernet1/0/5"}}},
//            {"name":"Server-1","id":"PTT...","ip":"192.168.50.10","type":"Server"}]}}
```
> PT 的路径追踪有官方已知限制：**只返回部分中间设备/端口**（实测原始返回
> PC-JX1 → Core-3650 → Server-1，中间的 SW-JX-2960 未出现）。W10.1 通过 topology + hosts
> 构图 BFS 补全后，同一请求返回 **4 跳**：
> `PC-JX1 → SW-JX-2960 → Core-3650 → Server-1`（补全的 SW-JX hop 标
> `synthetic=true`，上联端口 `FastEthernet0/24`；SW→Core 段匹配真实链路
> `link_id=795198557`）。**缺失的端口**（如主机接入口的交换机侧端口 NC `/host` 不提供）
> 仍显示 `UNKNOWN`，不做臆造。

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/path-trace `
  -H "content-type: application/json" `
  --data '{\"source_ip\":\"192.168.20.10\",\"dest_ip\":\"192.168.50.10\"}'
# → {"ok":true,"trace_status":"complete","synthetic_hop_count":1,
#    "hops":[{"index":0,"device_name":"PC-JX1","role":"source","synthetic":false,...},
#            {"index":1,"device_name":"SW-JX-2960","role":"transit","synthetic":true,
#             "source":"topology_inference","ingress_port":"UNKNOWN",
#             "egress_port":"FastEthernet0/24"},
#            {"index":2,"device_name":"Core-3650","role":"transit","ingress_port":"Vlan20",...},
#            {"index":3,"device_name":"Server-1","role":"destination",...}],
#    "links":[{"source_name":"PC-JX1","target_name":"SW-JX-2960","reason":"host_attachment","synthetic":true},
#             {"source_name":"SW-JX-2960","target_name":"Core-3650","link_id":"795198557","reason":"topology","synthetic":false},
#             {"source_name":"Core-3650","target_name":"Server-1","reason":"host_attachment","synthetic":true}],
#    "elapsed_ms":1014,"cached":false,
#    "warnings":["中间设备 SW-JX-2960 由拓扑推断补全（NC 未返回）",...]}
```

### 2) 告警根因分组：`GET /api/alerts/groups?state=active`

- **归组**：同链路/同设备告警归一组；根因优先级
  `link_down > device_unreachable > device_offline > host_down > health_issue > other`；
- **下游抑制**：根因为 `link_down`（链路两端设备）或 `device_unreachable`（该设备）时，
  其下游设备/主机告警组会被吸收到根因组，成员标 `suppressed_by=<group_id>`，
  `suppressed_count` 计数；**仅展示层抑制：不删除、不改变告警 active 状态**；
- 组字段：`group_id`（稳定 hash）/`root_cause`/`root_device_id`/`root_device_name`/
  `severity`/`title`/`alert_ids`/`member_count`/`suppressed_count`/`first_seen`/
  `last_seen`/`members[{id,alert_type,severity,device_name,detail,suppressed_by,...}]`；
- `POST /api/alerts/groups/{group_id}/ack` 整组确认（含被抑制成员），写审计
  `alert.group_ack`。

```powershell
curl.exe "http://127.0.0.1:8000/api/alerts/groups?state=active"
curl.exe -X POST http://127.0.0.1:8000/api/alerts/groups/g_xxx/ack -H "X-Operator: web-ui"
```

### 3) Webhook 告警通知

- 仅在**新产生 active 告警**且 severity 达到 `NOTIFY_MIN_SEVERITY` 时发送
  （cleared/info 不发；基线轮不发；`status=silenced` 不发）；同 `dedup_key` 5 分钟去重；
- 后台 asyncio 队列 + worker 发送，**不阻塞 5s 采集循环**；失败重试 2 次（1s/3s 退避）；
- 消息格式：`generic` 为
  `{title,severity,device,time,detail,alert_id}`；`dingtalk/wecom` 为 markdown，
  `feishu` 为 text，均带告警标题/级别/设备/时间/详情；
- `GET /api/notify/status` 暴露 enabled/type/min_severity/webhook_url(脱敏)/
  last_sent_at/last_error/sent_count/failed_count/pending；`POST /api/notify/test`
  立即发送测试消息。

```powershell
$env:NOTIFY_ENABLED='true'
$env:NOTIFY_WEBHOOK_URL='http://127.0.0.1:9000/webhook'
$env:NOTIFY_WEBHOOK_TYPE='generic'
$env:NOTIFY_MIN_SEVERITY='warning'
```

### 4) SNMP 真实设备驱动（`DRIVER=snmp`）

- 实现既有 `Driver` 协议（`name/probe/collect/close`），MIB-II v1/v2c：
  `sysDescr.0`/`sysUpTime.0`/`sysName.0` + `ifDescr`/`ifOperStatus`/`ifSpeed`/
  `ifHighSpeed`/`ifHCInOctets`/`ifHCOutOctets`（64 位优先，32 位兜底）；
- 归一化为 `Device`（hostname/mgmt_ip/platform 由 sysDescr 解析）+ `MetricPoint`
  （`if_in_bps`/`if_out_bps`/`if_utilization_pct`/`device_utilization_pct`），
  **`source="snmp"`**，端口指标带 `speed_bps/clamped` 且保证 `bps <= speed_bps`；
- 容错：单设备超时/失败只影响该设备（`errors` 记录），全部失败 `ok=false` +
  `reachable=false` 优雅降级；`SNMP_DEVICES=[]` 时返回空数据集、服务正常。

```powershell
$env:DRIVER='snmp'
$env:SNMP_DEVICES='[{"name":"SW-A","ip":"192.168.10.11","community":"public","version":"2c","building":"teach"}]'
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m uvicorn app.main:app --port 8000
```

> 依赖：`pysnmp==4.4.12` + `pyasn1==0.4.8`（pysnmp 4.x 的稳定同步 hlapi；已写入
> requirements.txt 并通过阿里云镜像安装）。驱动内部用 `asyncio.to_thread` 包装同步
> SNMP 调用，避免阻塞事件循环；单测通过注入 mock SNMP 客户端，不需要真实设备。

## WebSocket 协议（`/ws`）

连接成功后**先立即推一帧全量快照**，之后每次采集轮：

```jsonc
// 1) 全量快照（每个采集周期）
{"type": "snapshot", "data": {
  "captured_at": 1793346000.1, "observed_at": 1793346000.1,
  "observed_at_iso": "...", "hosts_observed_at": 1793346000.1,
  "driver": "netcon", "source": "netcon", "stale": false,
  "counts": {"devices": 5, "links": 4, "hosts": 6, "issues": 0, "metrics": 45},
  "devices": [...], "topology": {...}, "hosts": [...], "issues": [...],
  "network_health": {...}
}}

// 2) 新告警（仅新出现的 active/info 事件，逐条）
{"type": "alert", "data": {
  "alert_type": "host_down", "severity": "critical", "state": "active",
  "title": "主机离线：PC-JX1", "detail": "...", "dedup_key": "host_down:PTT...",
  "device_id": "...", "observed_at": 1793346000.1, "source_ts": "2026-10-30 15:00:00",
  "created_at": 1793346000.1, "source": "netcon", "acknowledged": false,
  "resolved_at": null, "clear_reason": null, "stale_source": false
}}

// 3) 恢复摘要（同一轮多条恢复合并为一条，不逐条刷屏）
{"type": "alerts_cleared", "data": {
  "count": 2, "stale_source_count": 1, "observed_at": 1793346163.5,
  "summary": "本轮清除 2 条告警（其中 1 条因 NC 问题记录滞后，已按 host/device 实际状态判定恢复）",
  "items": [{"dedup_key": "...", "alert_type": "health_issue", "stale_source": true,
             "source_ts": "2026-10-29 21:33:14", ...}]
}}

// 4) 数据源状态变化（首次进入 stale 时）
{"type": "status", "data": { /* 同 /api/health */ }}

// 5) 心跳：客户端发 "ping"，服务端回 {"type": "pong", "ts": ...}
```

告警类型：`host_down` / `device_unreachable` / `device_offline` / `link_down` /
`health_issue`（active）；一次性 info：`device_online` / `link_up`。

## 数据来源标注（重要）

- **真实采集**：NC 拉到的设备、拓扑、主机、健康问题、健康总览 → `source="netcon"`。
- **仿真补充**：接口/设备级指标（`if_in_bps` / `if_out_bps` / `if_utilization_pct` /
  `cpu_pct` / `mem_pct` / `device_utilization_pct`）→ **每条 `MetricPoint.source="simulator"`**，
  `/api/metrics/{device_id}` 响应顶层也标 `"source": "simulator"`。
- **拓扑链路例外**：`Link.source` / `Link.target` 是两端设备 id（沿用 NC 链路字段语义），
  链路的数据来源标在 `Link.data_source`（`netcon` / `simulator`）。
- 仿真模式下设备 hostname 统一带 `SIM-` 前缀，避免与真实设备混淆。

### 指标行为模型（`simulator.py`）

不是裸随机数，而是确定性行为模型（同一时间片重复计算结果一致）：

1. **分时段基础负载**：24 小时锚点曲线（凌晨低谷 / 上午下午高峰 / 午间回落 / 夜间衰减）
   线性插值，周末再乘衰减系数；
2. **拓扑感知流量分摊**：总需求按「链路容量 × 确定性权重（id 的 CRC32 相位）」分摊到
   活跃链路，核心链路负载高于接入链路；
3. **故障后流量重分布**：链路/设备故障后，其流量份额按权重转移给剩余活跃链路
   （其余链路利用率上升），故障端口指标归零；
4. **确定性扰动**：时间桶 + 相位的正弦微扰（±4.5%），不使用任何随机数。

## W3.1 数据口径修复（2026-10-30）

### 1) `/api/network-health` 字段自洽

**问题（8000 实例实测复现）**：PT NC 的 `/network-health` 只返回
`healthyNetworkDevice` 百分比，没有 `healthyDevices/unhealthyDevices` 计数；旧实现直接
透传后 API 出现自相矛盾：

```jsonc
// 修复前
{"total_devices": 5, "healthy_devices": 0, "unhealthy_devices": 0,
 "health_percentage": null, "healthy_devices_percentage": 100.0}
// AI Agent / 前端会据此判定“健康口径不可信”
```

**修复后**：`network_health` 字段分三组，权威口径由设备列表自算
（`source_of_truth="computed_from_devices"`）：

| 字段组 | 字段 | 说明 |
|---|---|---|
| **权威自算** | `device_total` / `device_healthy` / `device_unhealthy` / `device_unreachable` / `health_percentage` | 从当前设备列表计算；健康 = `collectionStatus=="Managed"` 且 `reachabilityStatus` 非 Unreachable；无设备时 `health_percentage=0.0` |
| **兼容旧字段** | `total_devices` / `healthy_devices` / `unhealthy_devices` / `unreachable_devices` / `healthy_devices_percentage` | 与自算字段同值，保证旧前端 / AI Agent 兼容 |
| **NC 原始字段** | `nc_total_devices` / `nc_unreachable_devices` / `nc_healthy_devices_percentage` / `nc_healthy_clients_percentage` / `nc_num_licensed_*` / `nc_summary` | 原样保留用于审计对比（不参与自算） |
| 顶层审计 | `device_count` / `source_of_truth` | 与 `/api/devices` 交叉校验 |

**不变式**：`device_healthy + device_unhealthy == device_total`；
`health_percentage` 恒为数值（不为 null）。实现位于
`models.NetworkHealth.compute_from_devices`，netcon 驱动与 pipeline 快照两处均强制自算。

### 2) simulator 指标物理约束

**问题（8000 实例实测复现）**：NC 端口速率数值单位是 **Kbps**（实测
`startPortSpeed=100000` → 100Mbps，`1000000` → 1Gbps），旧解析按 Mbps 处理导致链路
容量放大 1000 倍，simulator 生成的 `if_in_bps≈15.31e9`、`if_out_bps≈20.13e9`
远超 100Mbps 端口（1e8）—— 物理不可能。

**修复**：

1. `parse_speed_mbps`：裸数字按 **Kbps** 解释；带单位字符串（`Gbps/Mbps/Kbps/bps`）按单位解析；
2. `Link.port_speed_mbps(start)` 提供单端端口速率（Mbps）；
3. simulator 生成端口指标时以该端口 `speed_bps` 为上限钳制，保证
   `if_in_bps / if_out_bps <= speed_bps`、`if_utilization_pct <= 100`；
4. `MetricPoint` 新增 `speed_bps`（端口物理上限，bps）与 `clamped`（是否触顶被钳制），
   `/api/metrics` 每条指标可见，响应带 `physical_constraint` 说明；
5. 旧指标（schema v2）启动迁移时用最近一次拓扑快照回填 `speed_bps` 并钳制越界历史值
   （实测回填 3648 条、其中 2432 条被钳制），schema_version 2→3，历史告警/指标不丢。

```jsonc
// 修复后 /api/metrics 指标点
{"metric": "if_in_bps", "value": 16128386.85, "unit": "bps",
 "speed_bps": 100000000.0, "clamped": false, "source": "simulator", ...}
```

## 容错与降级设计（对照需求）

| 场景 | 行为 |
|---|---|
| NC 未启动 / 端口不通 | 启动探测失败只打 warning，服务照常起来；每轮采集返回空数据（`ok=False`），接口返回空结构；**指数退避**（15s→30s→…→120s 封顶）后才重新尝试，不会 5 秒一次空转，不会卡死 |
| NC 在线但 0 设备 | 端点返回 `{"version":"1.0"}`（无 response 字段）→ 归一化成空列表，不是错误 |
| ticket 过期 | 401/403 自动重登并重试一次；idle 900s/session 3600s 到期前（80%）主动续期 |
| 单端点 4xx/5xx | 只影响该端点，其余数据照常返回；`errors` 记录失败端点；设备/主机/拓扑端点失败时**跳过对应检测规则**，防止误报“全部离线/全部恢复” |
| 采集卡住 | 单轮 `asyncio.wait_for(collect_timeout=30s)` 硬超时，超时降级后继续下一轮 |
| 数据源中途离线 | 保留最近一次成功快照并标 `stale=true`，下一轮恢复后自动更新；active 告警保持（状态未知不误清除） |
| NC issue 清除滞后 | 自建规则按 host/device 实际状态恢复，`stale_source` 标记 NC 记录滞后；抑制重复告警 |
| 告警噪声（11 条集中清除） | incident 生命周期去重 + 同一轮恢复聚合成一条摘要 + 历史 TTL |
| WebSocket 客户端异常 | 单连接发送失败即剔除，不影响其它客户端与服务 |

## 持久化

SQLite（WAL 模式，`backend/data/campus_twin.db`，已 gitignore）：

| 表 | 内容 |
|---|---|
| `device_snapshots` / `host_snapshots` | 最近一轮设备/主机快照（每轮覆盖式刷新） |
| `topology_snapshots` | 拓扑快照（内容哈希变化才追加，避免 5s 轮询写爆） |
| `health_issues` | 健康问题时间线（`first_seen` / `last_seen` / `active`） |
| `alerts` | 告警 incident（`state` active/cleared/info、`resolved_at`、`clear_reason`、`stale_source`、`observed_at`、`source_ts`）；W9 起含 `deleted_at`（软删除）/ `status`（open/silenced）/ `silenced_at`；旧库启动自动迁移（只 ADD COLUMN，不删历史） |
| `metric_points` | 指标时序（按保留时长定期清理）；W3.1 起含 `speed_bps`（端口物理上限）/ `clamped`（触顶钳制标记），旧库启动自动回填/钳制 |
| `scene_config` | W9 场景配置单行 JSON（`version` 每次写 +1；首次访问 seed 4 楼栋/12 设备） |
| `audit_log` | W9 操作审计（operator/action/target/detail/result/created_at） |

## 测试

```powershell
cd D:\zdsh\campus-twin\backend
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pytest
```

全部单测**离线**（固定 JSON fixture / `httpx.MockTransport`），不依赖 PT 在线。
> 注意：验证实例请用独立 `DB_PATH`（如 `$env:DB_PATH='...\data\verify.db'`），
> **不要删除 `backend/data/campus_twin.db`**（生产库，含历史告警/指标）。

| 文件 | 覆盖点 |
|---|---|
| `test_netcon_normalize.py` | 字段归一化、空响应容错、ticket 认证/缓存/续期、401 重登、连接失败退避、单端点隔离、PT 真实响应形态回归 |
| `test_simulator.py` | 全部数据 `source="simulator"`、确定性、分时负载、故障后重分布、无裸随机；**W3.1 物理约束：100M/1G 端口下 bps<=speed_bps、util<=100%、钳制 helper、speed_bps/clamped 字段** |
| `test_detection_rules.py` | W3 四类规则 + 恢复判定：host_down / device_unreachable / link_down / health_issue；**NC issue 仍在但 host 已恢复 → stale_source cleared**；防 flap 抑制 |
| `test_host_freshness.py` | **A：每轮重新拉 /host（两轮调用两次）、SUCCESS→FAILURE 立即反映、no-cache 头 + `_ts` cache-buster、pipeline 快照/持久化刷新**；时间基准归一化 |
| `test_alert_noise.py` | **C：active 去重、恢复只改状态不新增行、多恢复聚合成一条摘要、TTL 只清 cleared/info、旧库 schema 迁移（v1→v2→v3、历史指标 speed_bps 回填与钳制）** |
| `test_stale_source_replay.py` | **E：fixture 回放故障→恢复场景**（host 恢复但 NC issue 仍在）→ WS `alerts_cleared` 摘要 + stale_source 标记 + 多轮不 flap |
| `test_pipeline.py` | 首轮基线静默持久化、poll_once 持久化 + 广播、失败标记 stale |
| `test_store.py` | WAL、快照/告警/指标读写（含 speed_bps/clamped）、拓扑去重、健康问题活跃状态、指标清理 |
| `test_network_health.py` | **W3.1：Managed+Reachable 健康口径、device_healthy+unhealthy==total、percentage 非 null、NC 异常返回归一化、NC 原始字段并存、pipeline 快照强制自算** |
| `test_api.py` | 仿真/不可达模式全接口、告警生命周期 API、检测规则 API、WebSocket（快照/告警/ping-pong） |
| `test_alerts_manage.py` | **W9：软删除/恢复、active 不可删不变式、clear 两种 scope + before、静默 + status 过滤、审计记录** |
| `test_scene_api.py` | **W9：scene seed 契约（4 楼栋/12 设备）、楼栋/设备 upsert + 碰撞自动偏移、confirm、delete tombstone、reset、新设备 pending 与三级归属推断** |
| `test_audit.py` | **W9：审计格式/过滤/limit/默认 operator、被拒绝操作审计、schema v3→v4 迁移保留历史告警 + scene seed + 新表** |
| `test_path_trace.py` | **W10：NC flow-analysis 归一化（hops/roles/ports）、缺失端点合成、NC MockTransport POST+轮询、超时/不支持降级、2 分钟缓存、API 契约** |
| `test_alert_groups.py` | **W10：根因优先级、link_down 下游抑制、独立组、稳定 group_id、整组 ack + 审计、API** |
| `test_notify.py` | **W10：本地 HTTP 接收器实测 generic 载荷、严重级/状态静默过滤、dedup、失败重试 2 次、多种消息格式、状态/测试 API、pipeline 触发** |
| `test_snmp_driver.py` | **W10：mock SNMP 采集归一化（source=snmp、speed_bps/clamped、bps<=speed）、计数器差值、单设备失败隔离、全部失败降级、无设备降级、DRIVER=snmp 切换** |

## 自测记录

### W10.1（2026-10-30，本机 Windows / Python 3.9.9）

```powershell
cd D:\zdsh\campus-twin\backend
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pytest
# => 171 passed in ~14s（新增拓扑补全用例：部分 hop 补全 / 完整 hop 不重复 / 拓扑不可达降级）
```

**8001 独立实例实测**（真实 NC + 生产库副本；全程未动 8000）：

| 验证点 | 结果 |
|---|---|
| `192.168.20.10 → 192.168.50.10` 路径补全 | 1.04s 返回 **4 hops**：hop0 PC-JX1(source) → hop1 **SW-JX-2960(synthetic=true, source=topology_inference, ingress=UNKNOWN, egress=FastEthernet0/24)** → hop2 Core-3650(NC 原始, ingress=Vlan20, egress=GigabitEthernet1/0/5) → hop3 Server-1(destination)；`synthetic_hop_count=1` |
| links 同步补齐 | 3 条：PC→SW `reason=host_attachment, synthetic=true`；SW→Core `link_id=795198557, reason=topology, synthetic=false, source_port=FastEthernet0/24`；Core→Server `reason=host_attachment, synthetic=true` |
| warnings | 含“中间设备 SW-JX-2960 由拓扑推断补全（NC 未返回）”；另有“部分 hop 间无真实拓扑链路（主机接入/推断段），前端按 hop 顺序连线” |
| 缓存 | 第二次调用 `cached=true, wall≈0.00s, hop_count=4`（缓存逻辑不变） |
| 8000 老实例 | 全程 `LISTEN`（pid 不变），未被触碰；验证后 8001/临时库已清理 |

### W10（2026-10-30，本机 Windows / Python 3.9.9）

```powershell
cd D:\zdsh\campus-twin\backend
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pytest
# => 169 passed in ~12s（新增 test_path_trace / test_alert_groups / test_notify / test_snmp_driver）
```

**8001 独立实例实测**（真实 NC + 生产库副本 / simulator + 本地 webhook 接收器 /
DRIVER=snmp；全程未动 8000）：

| 验证点 | 结果 |
|---|---|
| 真实 NC 路径追踪 `192.168.20.10 → 192.168.50.10` | 第一次调用 1.04s 返回 `ok=true, trace_status=complete`：hop0 PC-JX1(source) → hop1 Core-3650(transit, ingress=Vlan20, egress=GigabitEthernet1/0/5) → hop2 Server-1(destination)；2 条合成 links（PT 只返回部分设备，`synthetic=true`，warnings 说明）；第二次调用命中缓存 `cached=true, wall≈0.00s` |
| 告警根因分组（simulator 注入设备故障） | 2 个 link_down 根因组；其中 `g_9028ee4a2c88`（root=SIM-DIST-SW2）`member_count=1, suppressed_count=2`，被抑制成员 `device_unreachable`/`health_issue` 均带 `suppressed_by`；`POST .../ack` → `{"ok":true,"acknowledged":3}`，audit `alert.group_ack` 记录 operator=agent |
| Webhook（本地 Python HTTP 接收器 127.0.0.1:9000） | `NOTIFY_ENABLED=true, min_severity=warning`：`POST /api/notify/test` → ok=true；注入设备故障后收到 4 条新 active 告警消息（device_unreachable/link_down×2/health_issue），消息含 title/severity/device/time/detail/**alert_id（1~4，入库后回填）**；`/api/notify/status` sent_count=4、failed_count=0、last_error=null |
| SNMP 驱动（无设备） | `DRIVER=snmp, SNMP_DEVICES=[]`：`/api/health` status=ok、driver=snmp、devices=0、reachable=false；服务不崩溃；`/api/path-trace` 返回中文“当前驱动不支持路径追踪” |
| SNMP 驱动（配置不可达设备） | 配置 127.0.0.1:161（无 agent）：health status=ok、stale=true、devices=0、`last_error=全部 SNMP 设备采集失败：…No SNMP response received before timeout`；服务保持可用 |
| 8000 老实例 | 全程 `LISTEN`（pid 不变），未被本任务触碰；验证后 8001/9000/临时库已清理 |

### W9（2026-10-30，本机 Windows / Python 3.9.9）

```powershell
cd D:\zdsh\campus-twin\backend
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pytest
# => 129 passed in ~5s（新增 test_alerts_manage / test_scene_api / test_audit + 迁移测试）
```

**8001 独立实例实测**（两轮：生产库副本 + 真实 NC；全新库 + simulator；全程未动 8000）：

| 验证点 | 结果 |
|---|---|
| DB 迁移 v3→v4（生产库副本，19 条历史告警） | `schema_version=3→4`；告警 19 条全保留；新增 `scene_config` / `audit_log`；首次 `GET /api/scene` seed 4 楼栋/12 设备（version=1） |
| `/api/scene` | 4 楼栋：core(0,0)/teach(-120,40)/train(120,40)/dorm(0,-140)，设备数 4+3+2+3=12；设备字段 `name/type/position/status/source` 完整 |
| `/api/scene/pending`（真实 NC） | 5 台真实设备均在默认场景中 → `count=0`；simulator 实例 6 台 SIM-* 未纳管 → `count=6`，SIM-CORE-R1 推断 `center/core`（hostname_keyword） |
| 告警软删/恢复/清空 | 生产副本 id=19（cleared）：`DELETE` → `{"deleted":1}`，默认列表消失、`include_deleted=true` 可见 `deleted_at`；`restore` → `{"restored":1}`；`clear scope=cleared` → `{"deleted":19}`；`summary` 归零 |
| active 不可删 + 静默（simulator） | 注入设备故障 → 4 条 active；`DELETE /api/alerts/4` → **409**；`silence` → `{"silenced":1,"status":"silenced","state":"active"}`；静默后 DELETE 仍 409 |
| scene 写操作（simulator） | 新增楼栋 `bld-5`（pending，version 2）；同坐标添加两台设备 → 第二台 `adjusted=true, message=坐标与已有设备冲突，已自动偏移 x+8`；confirm → confirmed；DELETE → tombstone；reset → version=7、4 楼栋/12 设备 |
| `/api/audit` | 9 条操作全部记录（operator=agent/web-ui；`alert.delete` 被拒记录 `result=rejected`） |
| 8000 老实例 | 全程 `LISTEN`（pid 不变），未被本任务触碰；验证后 8001/临时库已清理 |

### W3.1（2026-10-30，本机 Windows / Python 3.9.9）

```powershell
cd D:\zdsh\campus-twin\backend
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pytest
# => 107 passed in ~3s（新增 test_network_health.py + 物理约束/迁移测试）
```

**8001 独立实例实测**（用生产库副本验证 v2→v3 迁移 + 真实 NC；全程未动 8000）：

| 验证点 | 结果 |
|---|---|
| `/api/network-health`（修复前 8000/修复后 8001） | 修复前：`healthy_devices=0, unhealthy_devices=0, health_percentage=null, healthy_devices_percentage=100`；修复后：`device_total=5, device_healthy=5, device_unhealthy=0`（sum=5）、`health_percentage=100.0`（非 null）、legacy 字段同值、`nc_total_devices=5 / nc_healthy_devices_percentage=100` 原始字段并存、`source_of_truth=computed_from_devices` |
| `/api/metrics` 物理约束 | 对 5 台设备的 2671 个端口指标点全量校验：**bps<=speed_bps 违规 0 条**；1717 条为迁移回填钳制点（`clamped=true, speed_bps=1e8`）；新生成点 `if_in_bps≈1.6e7 <= 1e8`（约 16% 利用率），物理自洽 |
| DB 迁移 | schema v2→v3：告警 4 条全保留、`/api/alerts/summary` 正常；历史指标回填 `speed_bps` 3648 条（其中 2432 条超限被钳制），无历史行删除；`/api/health.detection` 正常（engine=self-rules-v1） |
| 8000 老实例 | 全程 `LISTEN`（pid 不变），未被本任务触碰 |

### W3（2026-10-30，本机 Windows / Python 3.9.9）

```powershell
cd D:\zdsh\campus-twin\backend
& 'D:\zdsh\campus-twin\.venv\Scripts\python.exe' -m pytest
# => 96 passed in ~3s
# 另：python -m compileall app tests 通过；pyflakes 无告警
```

**实测 1：真实 NC（PT 已开，58000，5 设备 / 6 主机 / 0 健康问题）** —— 新代码在 **8001**
端口起独立测试实例（未动 8000 旧进程）：

| 验证点 | 结果 |
|---|---|
| `/api/health` | `status=ok`，driver=netcon，reachable=true，devices=5、hosts=6、issues=0，`detection.engine=self-rules-v1` |
| `/api/hosts` 与 NC `/host` 一致性 | 6 台主机逐条比对 **ping_status mismatch=0** |
| 主机新鲜度 | `hosts_observed_at` 随每轮采集推进（6s 后 +5.0s）；`polls=4` 时 `host_fetch_count=4`（每轮都真的拉了 /host） |
| 健康网络防误报 | 连续多轮 `alert_states.active=0`（无虚假告警） |
| 检测规则目录 | `/api/detection/rules` 返回 7 条结构化规则 + 时间基准说明 |

**实测 2：fixture 回放“NC issue 滞后但 host 已恢复”**（离线 Mock NC，2s 轮询）：

| 阶段 | 观测 |
|---|---|
| 故障阶段 | `/api/hosts`：PC-JX1 `ping_status=FAILURE`；`/api/issues` 1 条（`source_ts=2026-10-29 21:33:14`，`observed_at=`宿主机时间）；`/api/alerts?state=active` 2 条：`host_down` + `health_issue`，均 `critical` |
| 恢复阶段（NC issue 仍在） | `/api/hosts`：PC-JX1 `ping_status=SUCCESS`；`/api/issues` 仍 1 条但 `stale_source=true`；WebSocket 收到 **1 条** `alerts_cleared` 摘要：`count=2, stale_source_count=1`，其中 health_issue 的 clear_reason = “NC issue 仍存在，但数据源实际状态已恢复…NC 问题记录滞后，数据源已恢复”；`host_down` 的 clear_reason = “pingStatus FAILURE -> SUCCESS” |
| 多轮稳定性（防 flap） | 恢复后继续轮询到 `polls=15`：`active=0, cleared=2` 稳定不变（NC issue 仍持续返回也不会反复告警）；累计标记的 cleared 行没有增加 |

**实测 3：仿真故障闭环（回归）** —— 注入 link/device 故障 → 告警 WebSocket 实时推送 →
reset → 恢复被聚合为一条摘要；历史告警生命周期/`state` 过滤正常。

> 8000 端口旧进程未被本任务触碰；W3 验证实例均使用 8001 + 独立 `DB_PATH`，验证后已关闭。

## 已知限制 / 下一步

1. **NC 只允许 localhost**：后端必须与 PT 跑在同一台机器（Real World Access 的安全限制）。
2. **指标是仿真数据**：PT REST 不暴露接口计数器；若将来需要真实端口计数，需要 PT
   设备侧导出/`show` 命令解析，届时新增驱动即可（`MetricGenerator` 可替换）。
3. 健康问题严重级别由问题文本关键词推断（NC 未提供 severity 字段），后续可接规则引擎细化。
4. **stale_source 抑制**：为避免 NC 长期不清 issue 造成反复告警，已按 stale 恢复的
   issue 在 NC 记录仍返回期间不再告警；若需要“强制重新核对”能力，可后续加手动 reset 接口。
5. 告警目前为规则级联（单条数据源状态 → 单条告警）；跨设备根因关联/拓扑影响面分析属于
   W5–6 AI 运维 Agent 的工作，可在本引擎输出之上扩展。
6. 单进程内存态 + SQLite；多实例部署需要换消息总线/集中存储（当前 MVP 不需要）。
7. **历史指标口径**：v3 迁移前的旧指标点只有存在最近拓扑快照上下文时才能回填
   `speed_bps`；若旧库没有拓扑快照，历史点 `speed_bps` 仍为 null（不臆造上限）。新指标总是带
   `speed_bps/clamped`。
8. **端口速率单位**：按 Cisco DNA Center / PT NC 的 Kbps 口径解析裸数字；若将来接入其它
   单位的数据源，需要给 `parse_speed_mbps` 传对应 `default_unit` 或在驱动层归一化。
9. **W9 场景写并发**：scene_config 为单行 JSON + 单进程读改写，多 worker/多实例部署时
   需要加分布式锁或改行级存储（当前 uvicorn 单进程足够）。
10. **新设备检测范围**：目前只比对 inventory（NC /network-device）中的网络设备；终端
    hosts 的新增（NC /host）暂不进入 pending 队列，由场景/Agent 预置或手动 upsert。
11. **场景删除是版本化软删除**（tombstone 保留在 `removed_devices`），硬删除仅由后续
    维护任务处理；同一设备名重新 upsert 会自动清掉 tombstone。
12. **PT 路径追踪不完整**：NC 只返回部分中间设备/端口（官方限制）。W10.1 已用 topology +
    hosts 构图 BFS 补全中间设备 hop（标 `synthetic=true`，links 带 `reason`）；但主机接入的
    交换机侧端口等 NC 不提供的字段仍为 `UNKNOWN`，精确逐跳端口需 PT 侧增强或配置解析补充。
13. **Webhook 通知状态在内存中**：`sent_count/last_error/dedup` 随进程重启清零（告警本身在 DB）；
    如需持久化通知记录可后续加表。SNMP 驱动当前支持 v1/v2c（community），v3/USM 未实现。
