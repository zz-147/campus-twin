# W9 设计：多 Agent 扩展 + 场景自动化 + 数据管理（2026-10-30）

> 目标（用户需求确认版）：
> 1. 用户在 **PT 里手动**加设备 → 系统**自动发现** → 大屏**自动新增设备和楼栋**（不需要改代码；不调整已有楼栋外观）
> 2. 大屏上的**历史故障记录**、**对话历史**可以删除：既要有**按钮**（不依赖 Agent），也要能**一句话让 Agent 删**
> 3. 多 Agent 架构：**1 主 Agent + 4 专职子 Agent**

---

## 一、Agent 角色（5 个）

| Agent | 职责 | 输出 |
|---|---|---|
| **主 Agent（对话窗口显示它）** | 理解意图 → 拆任务 → 派给子 Agent → 汇总 → 生成「操作确认卡片」 | 最终答复 + actions[] |
| **接入引导 Agent**（onboarding） | 用户想加设备时：选 PT 型号、端口级连线表、完整 CLI 配置、验证命令 | Markdown 步骤 + `apply_scene_device` action |
| **场景构建 Agent**（scene） | 决定新设备/新楼栋在 3D 场景中的位置、尺寸、配色、图标（复用现有低模风格） | scene config JSON + action |
| **接入侦察 Agent**（recon） | 每轮比对后的新设备归属推断：拓扑邻居楼栋 → hostname 规则 → 管理 IP/VLAN → 兜底询问用户 | `new_device` 事件 + 推断结果 |
| **数据治理 Agent**（steward） | 删告警历史、静默活跃告警、删对话历史、审计查询、撤销 | actions（危险操作需二次确认） |

**实现方式**：单服务多角色（`agent/app/agent/roles/*.py` + `orchestrator.py`），后续可拆服务。
主 Agent 只做路由与汇总；子 Agent 各自独立 prompt + 工具白名单。

---

## 二、自动发现 → 场景注入流程

```
后端采集（5s）
  └─ 对比 scene config：发现 inventory 里有、scene 里没有的设备
       └─ 生成 new_device 事件（队列/Pending）
            └─ 接入侦察 Agent 推断归属（五级规则）
                 ├─ 成功 → 场景构建 Agent 生成 placement → 写入 scene config → 前端渲染
                 └─ 无法推断 → 大屏/对话提示"发现新设备 X，属于哪栋楼？" → 用户一句话确认
```

**归属推断规则（按优先级）**
1. 拓扑邻居：新设备连到的已纳管设备 → 继承其所属楼栋
2. hostname 关键词：`JX/教学`→教学楼、`SX/实训`→实训楼、`SS/宿舍`→宿舍楼、`CORE/机房`→中心机房
3. 管理 IP / VLAN 段映射（可配置表：`192.168.20.0/24→教学楼` 等）
4. 新楼栋：设备无法归入现有楼栋且用户确认"新建楼栋" → 场景 Agent 生成楼栋
5. 兜底：主 Agent 在对话中询问用户

**新设备默认状态**：`pending`（前端以浅色/虚线显示 + 提示"新设备已自动加入 X 楼，确认/撤销"），用户点确认后转 `confirmed`。

---

## 三、后端新增 API（backend/）

### 3.1 告警数据管理（软删除 + 审计 + 可撤销）
| 方法 | 路径 | 说明 |
|---|---|---|
| DELETE | `/api/alerts/{id}` | 软删除单条（`deleted_at`，审计） |
| POST | `/api/alerts/clear` | body `{"scope":"cleared"\|"all_cleared","before":epoch?}` → `{"deleted":N}` |
| POST | `/api/alerts/{id}/silence` | 静默活跃告警（不删除，`status=silenced`） |
| POST | `/api/alerts/{id}/restore` | 撤销软删除 |
| GET | `/api/alerts/audit` 或统一 `/api/audit` | 操作审计（who/action/target/time/detail） |

不变式：**active 告警不可删除**（只能 silence），避免误删活跃故障；硬删除仅 TTL 清理。

### 3.2 场景配置（配置驱动，替代前端硬编码 layout）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/scene` | `{version, theme, buildings:[{id,name,position,size,style,devices:[{name,type,position,status,source}]}]}` |
| POST | `/api/scene/buildings` | 新增楼栋（默认 `status=pending`） |
| POST | `/api/scene/buildings/{id}/devices` | 新增/更新设备位置（upsert） |
| POST | `/api/scene/devices/{name}/confirm` | 确认新设备（pending → confirmed） |
| DELETE | `/api/scene/devices/{name}` | 删除设备位置（审计） |
| POST | `/api/scene/reset` | 重置为默认 4 楼栋（审计） |
| GET | `/api/audit?limit=` | 审计列表 |

- 存储：SQLite `scene_config`（JSON）+ `audit_log`；每次变更 `version++`
- 初始数据：从现有 4 楼栋/12 设备生成默认 config（首次启动自动 seed）
- 所有写操作需 `X-Operator` 头（前端传 operator 名，默认 `web-ui`；Agent 传 `agent`）

---

## 四、Agent 服务扩展（agent/）

### 4.1 会话持久化
- SQLite：`conversations(id,title,created_at,updated_at)`、`messages(id,conversation_id,role,content,meta,created_at)`
- `GET /api/conversations`、`GET /api/conversations/{id}`、`DELETE /api/conversations/{id}`、`POST /api/conversations/clear`

### 4.2 Chat 返回 actions（前端渲染确认卡片）
```json
POST /api/chat {"question": "...", "conversation_id": "..."}
→ {
  "answer": "...",
  "trace": {...},
  "conversation_id": "...",
  "actions": [
    {"id": "act_x", "type": "clear_cleared_alerts", "params": {"before": null},
     "confirm_text": "将清空 9 条已恢复告警，确认执行？", "danger": true, "status": "pending"}
  ]
}
POST /api/actions/{id}/confirm → 执行并返回结果（执行前校验 action 未过期/未执行）
POST /api/actions/{id}/cancel
```
**action 类型**：`clear_cleared_alerts`、`delete_alert`、`silence_alert`、`delete_conversation`、`clear_conversations`、`apply_scene_device`、`apply_scene_building`、`confirm_scene_device`、`remove_scene_device`

**安全**：Agent 不直接执行写操作，必须用户在前端点「确认」；actions 10 分钟过期；全部写审计。

### 4.3 多角色编排
- `orchestrator.py`：主 Agent→子 Agent 计划（≤3 个子任务，可并行）→ 汇总 answer+actions
- 子 Agent 工具白名单：
  - onboarding：只读工具 + 配置模板库（不写）
  - scene：`get_scene` + `propose_placement`（纯计算，不写）
  - recon：`get_devices`/`get_topology`/`get_hosts`/`get_scene` + 推断
  - steward：数据管理工具（生成 action，不直接执行）
- 新增只读工具：`get_scene`、`get_audit`、`get_new_devices`、`infer_building`
- 评测集扩展：新增 10 个场景（加设备引导 / 归属推断 / 删告警 / 删对话 / 危险操作确认）

---

## 五、前端扩展（frontend/）

1. **配置驱动场景**：`layout.js` 改为从 `GET /api/scene` 加载；保留默认兜底；新楼栋用现有低模风格（半透明 box + 发光边框 + 中文 sprite 标签）；新设备用类型图标；`pending` 设备浅色/虚线 + 「新设备」角标。
2. **新设备提示**：顶部/侧栏「发现 1 台新设备：SW-XS-2960（已推断属教学楼）」+ [确认] [撤销] [查看配置]。
3. **告警管理 UI**：
   - 每条已恢复告警右侧「删除」按钮（确认弹窗）
   - 顶部「清空已恢复（N）」按钮
   - 活跃告警「静默」按钮（不删除）
   - 操作后 toast + 可撤销（10 秒内）
4. **对话历史 UI**：AI 面板顶部「历史会话」下拉/列表，支持删除单条、清空全部；当前会话保留。
5. **Agent action 卡片**：回答下方渲染 actions，危险操作红色 + 「确认执行 / 取消」，执行后显示结果。

---

## 六、失败处理与安全边界

| 场景 | 处理 |
|---|---|
| 新设备归属推断失败 | 主 Agent 询问用户；用户不回答则停用 pending（不自动建楼） |
| 新楼栋未经确认 | `pending` 状态不参与告警计数；确认后才算正式场景 |
| 删除误操作 | 软删除 + 审计 + 撤销接口；active 告警禁止删除 |
| Agent 生成的场景配置冲突 | 坐标碰撞检测（同楼同层最小间距），冲突时自动偏移并提示 |
| PT 配置失败 | 引导 Agent 提供 `undo` 命令清单与排查步骤 |
| Agent 超时/离线 | 前端降级：按钮操作仍可用（管理功能不依赖 Agent） |

## 七、验收清单

- [ ] 后端：告警删除/清空/静默/撤销 + 审计；场景 CRUD + 版本；pytest 全绿（独立 8001 验证）
- [ ] Agent：5 角色编排；会话持久化；actions 确认执行；评测集 30 场景（原 20 + 新 10）
- [ ] 前端：动态场景渲染（新增设备/楼栋）、新设备提示、告警删除/清空按钮、对话历史管理、action 卡片
- [ ] 端到端演示脚本：PT 加设备 → 自动出现 → 确认；删告警（按钮 + 一句话）；删对话
- [ ] 文档与截图更新
