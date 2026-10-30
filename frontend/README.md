# 校园网络数字孪生 · 3D 运维大屏 + AI 运维助手

面向校园网络运维场景的 **Three.js 3D 可视化大屏 + AI 运维对话面板**（Vite + 原生 JS，不引入 React）。
3D 场景、告警、实时数据全部来自同仓库后端 FastAPI（真实采集 Packet Tracer Network Controller）；
AI 问答来自同仓库 agent 服务（默认真实 DeepSeek，也可 mock 离线运行）。

W9 起场景**配置驱动**（`GET /api/scene`）：在 PT 里新增设备/楼栋会被后端自动发现并推送到大屏，
无需改前端代码；同时新增告警/会话管理 UI 与 Agent action 确认卡片。

W10 新增三项运维可视化能力：**路径追踪 3D 动画**（NC flow-analysis → 发光路径 + hop 编号 +
断点红色 ✕）、**告警根因分组**（同链路/同设备告警聚合、下游成员「被上游抑制」灰显）、
**Webhook 通知状态**（顶部状态 +「测试」按钮）。

W12 新增 **AI 流式对话（SSE）**：状态条（检索知识库/调用工具/生成回答）→ 逐字 token 渲染 →
完成后渲染 trace/actions（能力不变）+「停止生成」；**RAG 引用**（`[K1]/[K2]` 可点击角标 +
「参考知识」折叠区：标题/来源/分数）；流式接口 404/失败时**自动回退非流式**并提示。

W15 新增 **可观测性页签**：平均延迟 / 工具错误率 / Token 总量 3 个指标卡、最近 trace 列表
（点击展开事件序列、支持按当前会话过滤）、RAG 评测指标卡（recall@5 / MRR / 引用准确率 /
faithfulness）；Agent 接口未就绪时仅该页签优雅降级，不阻塞其它功能。

- 前端技术栈：Vite 8 + 原生 ES Module + Three.js 0.186 + OrbitControls
- 数据接入：REST 快照（首屏 / 兜底轮询）+ WebSocket `/ws` 实时推送（断线自动重连）+ 场景版本轮询（4s）
- AI 接入：`POST /api/chat`（非流式兜底）/ **`POST /api/chat/stream`（SSE 流式）**；
  真实模型单次 20~40s，前端超时 120s；支持 actions[] 与 RAG citations
- 可观测性：`GET /api/traces`、`GET /api/traces/{id}`、`GET /api/metrics/agent`、`GET /api/rag/report`
  （懒加载，仅打开页签时请求）
- 场景规模：默认 4 栋楼 / 12 个标记；W9 支持后端配置任意数量楼栋/设备（当前真实配置 v3 = 4 栋 / 12 设备）
- 性能：默认场景 45 个可渲染对象 / 65 个场景节点，无 LOD；路径追踪激活时额外约 17 个路径对象；
  流式 token 采用 80ms 节流粗渲染，AI 服务独立，不阻塞 3D 渲染

## 目录结构

```
frontend/
├─ index.html              # 大屏 DOM 骨架（状态栏 / 右侧信息坞 / 告警列表 / 新设备条 / 加载遮罩）
├─ vite.config.js          # Vite 配置（dev 5173 / preview 4173）
├─ package.json
├─ .env.example            # VITE_API_BASE / VITE_AGENT_BASE 示例
├─ docs/
│  ├─ screenshot-overview.png     # 真实后端数据总览
│  ├─ screenshot-detail.png       # 点击设备：聚焦 + 详情面板
│  ├─ screenshot-chat.png         # AI 运维助手：真实 DeepSeek 问答（Markdown 渲染）
│  ├─ screenshot-recovered.png    # W7.1：PC-JX1 恢复后为绿色，列表显示「已恢复 · NC 记录滞后」
│  ├─ screenshot-stale-bug.png    # W7.1 修复前对比：NC 滞后 issue 被误显示为活跃/红色
│  ├─ screenshot-w9-dynamic.png   # W9：配置驱动动态场景（5 栋楼 + pending 新设备提示条）
│  ├─ screenshot-w9-alerts.png    # W9：告警删除/静默/清空已恢复按钮
│  ├─ screenshot-w9-actions.png   # W9：Agent action 危险操作确认卡片（Mock 契约）
│  ├─ screenshot-w9-live-actions.png # W9.1：真实 Agent action 卡片（只取消未写入数据）
│  ├─ screenshot-w9-trace.png     # W9.1：多 Agent trace（plan[] 子任务 + 工具序列 + 耗时）
│  ├─ screenshot-w10-path.png     # W10：路径追踪 3D 发光路径 + hop 编号（完整路径）
│  ├─ screenshot-w10-path-broken.png # W10：断点路径（红色 ✕ 断点 + 红色最后一段）
│  ├─ screenshot-w10-groups.png   # W10：告警根因分组 + 被上游抑制成员灰显
│  ├─ screenshot-w10-path-live.png # W10：真实后端 NC flow-analysis 路径（3 跳）
│  ├─ screenshot-w12-streaming.png # W12：AI 流式对话（状态条/工具 chip/逐 token/停止生成）
│  ├─ screenshot-w12-rag.png      # W12：RAG 引用角标 + 参考知识（标题/来源/分数）
│  ├─ screenshot-w15-observability.png # W15：可观测性（指标卡/trace 展开/RAG 评测卡）
│  ├─ screenshot-ai-offline.png   # Agent 离线降级（不影响 3D 大屏）
│  ├─ screenshot-demo.png         # 后端离线 + 演示数据模式
│  └─ ai-call-record.md           # 对 :8100 真实 DeepSeek 的调用实测记录
├─ scripts/
│  ├─ verify-devices.mjs   # 场景设备名 ↔ 后端 /api/devices 一致性校验（Node 脚本）
│  ├─ smoke-test.mjs       # 端到端冒烟（headless Edge + CDP，四种模式：live/demo/ai-offline/w9）
│  ├─ smoke-w9.mjs         # W9/W10 冒烟快捷入口（SMOKE_MODE=w9）
│  ├─ mock-w9-server.mjs   # W9/W10 契约 mock：后端 8002 + Agent 8102（含 path-trace/groups/notify）
│  └─ cdp-lib.mjs          # 冒烟测试公共工具（CDP 封装，无第三方依赖）
└─ src/
   ├─ main.js              # 入口装配：数据流编排、场景配置、告警/新设备管理、AI 面板接线
   ├─ styles.css           # 深色科技风样式（无外部 CDN 字体）
   ├─ api/
   │  ├─ client.js         # REST 轮询 + WebSocket + 重连/降级 + 场景版本轮询
   │  ├─ agentClient.js    # Agent :8100 接入（流式 SSE / 问答 / actions / 会话 / RAG citations）
   │  ├─ adminClient.js    # W9 数据管理（告警删除/清空/静默/撤销、场景确认/撤销、审计）
   │  ├─ alertsView.js     # 告警列表/活跃语义纯函数（W7.1：stale_source → 已恢复·NC 滞后）
   │  ├─ normalize.js      # 后端字段归一化、状态四分类、observed_at 时间基准、场景配置
   │  └─ mock.js           # 内置演示数据（默认关闭，仅离线时手动启用）
   ├─ scene/
   │  ├─ CampusScene.js    # 3D 场景：动态楼栋/标记/链路/相机/拾取/新设备高亮
   │  ├─ sceneConfig.js    # W9 配置驱动场景模型（GET /api/scene 归一化 + 默认布局兜底）
   │  ├─ layout.js         # 默认 4 栋楼布局与设备→楼栋落位规则（纯 JS，向后兼容兜底）
   │  ├─ items.js          # 快照 + 场景配置 → 场景条目（归属/落位/告警匹配/pending，纯 JS）
   │  ├─ markers.js        # 设备几何体工厂（含 pending 虚线框/角标、AP/摄像头图标）
   │  └─ labels.js         # CanvasTexture 中文标签 Sprite
   └─ ui/
      ├─ hud.js            # 状态栏 / 告警列表（删除·静默·清空）/ 右侧页签坞
      ├─ aiPanel.js        # AI 对话面板（流式 SSE / Markdown / trace / 会话 / action 卡片 / RAG 引用）
      ├─ pathPanel.js      # W10 路径追踪面板（源/目的选择、hop 列表、断点/错误提示）
      ├─ observability.js  # W15 可观测性面板（指标卡/trace 列表展开/RAG 评测/懒加载降级）
      ├─ newDevices.js     # W9 新设备提示条（确认加入 / 撤销 / 选择楼栋）
      ├─ dialog.js         # 通用确认弹窗（危险操作二次确认）
      ├─ markdown.js       # 极简 Markdown 渲染器（标题/列表/代码块/表格/加粗，XSS 安全）
      └─ format.js         # 时间/百分比格式化与 HTML 转义
```

## 启动顺序（8000 → 8100 → 5173）

### 0. 环境要求

| 组件 | 要求 | 本机实测 |
|---|---|---|
| Node.js | ≥ 20.19（Vite 8 要求） | v24.16.0 |
| npm | ≥ 10 | 11.13.0 |
| 浏览器 | 支持 WebGL 的现代浏览器 | Edge |

npm 依赖走 npmmirror 镜像（官方源/GitHub 在网络受限环境不可用）：

```powershell
npm config set registry https://registry.npmmirror.com
cd D:\zdsh\campus-twin\frontend
npm install
```

### 1. 启动后端（:8000，数据源）

```powershell
# 确认 Packet Tracer 的 Network Controller 正在 127.0.0.1:58000 监听
cd D:\zdsh\campus-twin\backend
D:\zdsh\campus-twin\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

- NC 在线：采集真实设备/拓扑/主机/健康问题，前端显示「PT NC 真实采集」。
- NC 不在线：后端仍可启动，返回空数据并标记 `stale=true`；前端显示「未连接 / 无缓存」，
  不伪造设备（可打开「演示数据」开关预览界面，见下文）。

### 2. 启动 Agent 服务（:8100，AI 问答）

```powershell
cd D:\zdsh\campus-twin\agent
# 真实 DeepSeek（key 配置在 agent/.env 或环境变量 LLM_API_KEY）
D:\zdsh\campus-twin\.venv\Scripts\python.exe -m uvicorn app.api:app --port 8100

# 或离线 mock 模式（无需 key，用于演示/单测）：
$env:LLM_PROVIDER='mock'; D:\zdsh\campus-twin\.venv\Scripts\python.exe -m uvicorn app.api:app --port 8100
```

启动后可用 `GET http://127.0.0.1:8100/api/health` 检查 `provider_ready`。
**Agent 是否启动只影响 AI 页签**：未启动时前端显示「AI 服务未连接（127.0.0.1:8100）」，
3D 大屏、告警、实时数据全部照常工作。

### 3. 启动前端（:5173）

```powershell
cd D:\zdsh\campus-twin\frontend
npm run dev -- --port 5173
# 浏览器打开 http://127.0.0.1:5173/
```

生产构建：

```powershell
npm run build                    # 产物输出到 frontend/dist
npm run preview -- --port 4173   # 本地预览构建产物
```

### 4. 服务地址覆盖

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_API_BASE` | `http://127.0.0.1:8000` | 后端 FastAPI 地址 |
| `VITE_AGENT_BASE` | `http://127.0.0.1:8100` | AI Agent 服务地址 |

```powershell
$env:VITE_API_BASE='http://192.168.1.10:8000'
$env:VITE_AGENT_BASE='http://192.168.1.10:8100'
npm run dev -- --port 5173
```

也可复制 `.env.example` 为 `.env.local` 填写；修改后需重启 dev server 或重新 build。

## 页面功能

### 3D 大屏

- **4 栋楼**：中心机房 (0,0)、教学楼 (-120,40)、实训楼 (120,40)、宿舍楼 (0,-140)，
  半透明 BoxGeometry 体块 + 合并棱线 + 楼层线 + CanvasTexture 中文楼名。
- **设备标记**：交换机=小盒子、路由器=圆柱、PC=小方块、服务器=机架条、NC=发光八面体；
  状态颜色 `Managed` 绿 / `Unreachable` 红 / `Unsupported` 灰 / 无数据 蓝灰。
- **点击设备**：相机平滑聚焦 + 右侧「设备详情」页签（名称/类型/IP/型号/软件版本/采集状态/
  可达性/接口数/所属楼栋/**数据观测时间与快照延迟**/关联告警/数据来源）；点击空白或 `Esc` 取消。
- **告警脉冲**：只有 `/api/alerts?state=active` 中真正活跃的告警才让设备材质呼吸 + 红色光环
  （InstancedMesh 绘制）；NC 滞后的 `stale_source` issue 不触发红色/脉冲。
- **顶部状态栏**：健康度、设备在线、主机数、**活跃告警**、采集状态、**数据新鲜度（X 秒前）**、
  后端连接（REST/WS）、数据来源。新鲜度优先用 `/api/hosts` 的 `snapshot_age_seconds`，
  并按秒自行推进；tooltip 显示“数据更新于 X 秒前（后端 observed_at）”。
- **左下告警列表**：W3 生命周期标签（**活跃 / 已恢复**），时间优先展示后端 `observed_at`
  （宿主机采集时间），tooltip 同时展示 NC 原始 `source_ts`、恢复时间与 `clear_reason`；
  对 `stale_source=true` 的 NC 滞后 issue 显示为 **「已恢复 · NC 记录滞后」** 的灰黄次要标签，
  不计入活跃数、不触发 3D 红色/脉冲，tooltip 说明“NC 问题记录滞后，数据源实际状态已恢复”；
  点击告警可定位到对应设备；底部显示检测引擎（如 `self-rules-v1`）与生命周期统计（含 NC 滞后条数）。
- **右侧信息坞**：三个页签 —— 「设备详情」「AI 运维助手」「图例」；点击设备自动切到详情页签。
- **控制**：左键旋转 / 滚轮缩放 / 右键平移；自动旋转开关（聚焦时自动暂停）、刷新、复位视角、
  演示数据开关（默认关闭）。

### AI 运维助手（新增）

- 4 个快捷提问：**整体状态怎么样 / 哪个设备掉线了 / 教学楼连接不上 / 带宽是否拥塞**；
- 输入框 + 发送按钮，支持 **Enter 发送 / Shift+Enter 换行**；问题上限 2000 字；
- 加载态：「AI 正在分析…」+「已等待 X 秒」计时（真实模型通常 20~40 秒，前端超时 120 秒）；
- 回答渲染：标题、段落、加粗/斜体、行内代码、**列表、表格、引用、围栏代码块**（排障命令），
  长文本在面板内滚动；渲染前全量 HTML 转义，防止 XSS；
- 回答头部显示 `provider · model · 耗时`；**折叠 trace** 兼容两种 Agent 结构：
  - W9 多 Agent：`trace.plan[]` 每项展示子 Agent 角色/目标/`tools_used`/生成 action 数/耗时，
    顶部展示 `mode`（multi_agent）、`roles`、工具序列、工具调用/错误数、`stop_reason`、润色耗时；
  - 单 Agent / 旧结构：`trace.steps[]` 展示每步模型耗时、`tool_calls[]`（参数/耗时/返回字符数/错误）、
    `finish_reason`；工具调用数优先取 `tool_call_count`，缺失时按 `steps[].tool_calls` 或
    `plan[].tools_used` 求和，工具序列按 steps/plan 顺序展开；
- Agent 在线状态圆点（页签 + 面板内）：绿=已连接、红=未连接；离线时显示
  「AI 服务未连接（127.0.0.1:8100）」+「重试连接」，并禁用发送/快捷提问，
  但**不影响 3D 大屏、告警与实时数据**；
- provider 未配置（HTTP 503）、模型报错（HTTP 502）、超时等错误均有独立的中文提示。

### W9：配置驱动动态场景（自动新增设备/楼栋）

- 楼栋/设备位置优先来自 **`GET /api/scene`**（后端配置，`version` 版本号；当前真实配置 v3：4 栋楼 / 12 设备）；
  接口不可用或首次加载时回退 `layout.js` 的默认 4 栋楼布局，保证页面永远可用。
- 场景版本由前端 **4 秒轮询** `GET /api/scene` + `GET /api/scene/pending` 比对（`/ws` 可用时并行），
  版本变化 → 自动刷新 3D 场景，**无需手动刷新页面**；新增楼栋自动按低模风格渲染，新设备出现时高亮 3 秒。
- `status=pending` 的新设备：浅色半透明 + 黄色虚线外框 + 「新设备」角标；确认后恢复常规状态。
- 顶部「发现 N 台新设备」提示条：列出设备名 / 型号 / IP / 推断楼栋；
  - [确认加入] → `POST /api/scene/devices/{name}/confirm`（未推断楼栋时先选楼栋并 upsert）
  - [撤销] → `DELETE /api/scene/devices/{name}`
  - [配置] → 浏览器控制台输出该设备场景配置
- 「图例」页签展示当前场景来源（后端配置 vN / 默认布局）、楼栋数与设备数。

### W9：告警数据管理（不依赖 Agent 的按钮操作）

- 每条**已恢复**告警右侧「删除」按钮 → 二次确认 → `DELETE /api/alerts/{id}`（软删除 + 审计）；
  成功 Toast 提供 **10 秒内「撤销」**（`POST /api/alerts/{id}/restore`）。
- 底部告警面板顶部「**清空已恢复（N）**」→ 二次确认 → `POST /api/alerts/clear {"scope":"cleared"}`。
- **活跃**告警右侧「静默」按钮 → `POST /api/alerts/{id}/silence`（不删除，状态显示「已静默」）。
- 不变式：活跃告警不可删除（后端会拒绝）；所有写操作带 `X-Operator: web-ui` 并写入 `/api/audit`。

### W9：对话历史 & Agent action 确认卡片

- AI 面板顶部「历史会话」：会话列表（标题/时间/消息数）、点击加载、单条删除、清空全部（均二次确认）；
  Agent 服务会话接口未就绪时优雅降级为「暂不支持会话历史」，不影响问答。
- `/api/chat` 返回的 `actions[]` 渲染为确认卡片：危险操作**红色** + 「危险操作」标识 + `confirm_text` +
  `params`；[确认执行] → `POST /api/actions/{id}/confirm`（执行成功后刷新告警/场景数据），
  [取消] → `POST /api/actions/{id}/cancel`，卡片内展示执行/取消结果。
- 安全边界：Agent 不直接执行写操作，必须用户确认；卡片执行失败/过期时在卡片内展示错误信息。

### W9.1：新 Agent trace 结构兼容 & live action 冒烟

- 新 Agent 8100（W9 多 Agent）`/api/chat` 的 trace 不再使用旧 `steps[].tool_calls` 字段，
  改为 `plan[]`（子 Agent 编排：role/objective/tools_used/actions/elapsed_ms）+ 顶层
  `mode/roles/tool_sequence/tool_call_count/tool_error_count/actions_count/stop_reason/polish`；
  前端已做**结构自适应**（plan[] / steps[] / 仅 tool_sequence 三种结构都能渲染），
  页面工具调用数与「工具序列」顺序与后端 `tool_call_count` / `tool_sequence` 一致。
- `npm run smoke`（live）新增 **真实 Agent action 卡片联调**（只取消、绝不确认）：
  1. 问「我想在宿舍楼加一台 2960 接入交换机」→ 断言 `apply_scene_device`（non-danger）卡片 +
     确认/取消按钮，点击**取消**，随后校验 `/api/scene` 版本与楼栋配置、pending 数量未变化；
  2. 问「把已恢复的告警清空」→ 断言 danger 红色卡片 + `confirm_text`，点击**取消**，
     随后校验 `/api/alerts/summary` 与 `/api/audit` 无变化（19 条已恢复告警未被删除）。
- 冒烟同时校验 trace 摘要包含工具调用次数、trace body 含工具序列/子任务（兼容结构变化）。

### W10：路径追踪 3D 可视化

- 右侧信息坞新增「路径追踪」页签：源/目的下拉来自当前设备/主机/待确认设备（去重 IP），
  支持交换源/目的、追踪、清除；后端 `POST /api/path-trace`，NC 轮询最长 40s，前端显示「追踪中…」。
- 3D 场景高亮「演示效果」：
  - **发光路径**：CatmullRom 曲线光晕管 + 亮管 + 中心亮线（加法混合），2 个**方向脉冲**沿路径循环流动；
  - **hop 编号**：每个 hop 有地面→路径光柱、呼吸圆环、`1 PC-JX1 / 2 SW-JX-2960 …` 编号标签，
    相邻编号上下错开；路径激活时自动隐藏设备/楼栋文字标签，突出 hop；
  - **断点**：`trace_status=partial|failed` 时最后可达 hop 显示红色 ✕ 断点标签、红色脉冲环、
    红色光柱，最后一段路径变红；
  - 追踪完成自动把镜头拉到路径包围盒，数据刷新后自动重绘路径（不重复拉镜头）；
  - 面板同步展示 hop 列表（设备/角色/端口）、路径状态、耗时与中文错误；支持「清除路径」。
- 接口未就绪时显示「路径追踪服务未就绪（后端 /api/path-trace 未上线）」，不影响大屏其它功能。

### W10：告警根因分组视图

- 告警面板顶部切换「**全部 / 按根因分组**」；分组数据 `GET /api/alerts/groups?state=active`。
- 组卡片：根因标签（链路中断 / 设备不可达 / 主机离线…）、根因设备、严重级、成员数、被抑制数、首次时间；
  默认展开第一组，点击卡片头展开/收起。
- 成员列表：根因成员标「根因」，下游被抑制成员**灰显** + 标签「被上游抑制」+ `suppressed_by` 分组 id；
  被抑制只是展示层抑制，不删除、不改变 active 状态。
- 分组视图保留原有操作：成员告警仍可「静默 / 删除」，顶部「清空已恢复（N）」照常可用。
- 分组接口未就绪时降级显示普通告警列表 + 中文提示，不阻塞告警查看。

### W10：Webhook 通知状态

- 顶部状态栏「告警通知」：`GET /api/notify/status` → 已启用（类型 · 已发送 N 次）/ 未启用 / 未配置；
- 通知启用时显示「测试」按钮 → `POST /api/notify/test`，toast 显示发送成功/失败并刷新 sent_count；
- 接口未就绪时显示「未配置」并隐藏测试按钮。

### W12：AI 对话流式渲染（SSE）+ RAG 引用

- **流式气泡**：发送后先显示状态条（`正在理解问题… → 正在检索知识库… → 正在调用工具… → 正在生成回答…`）
  与工具 chip（如 `⚙ get_devices 12ms ✓`），随后 token 逐字追加（80ms 节流粗渲染 Markdown，
  避免全量重解析闪烁）；完成后完整渲染 Markdown + trace + action 卡片（现有能力不变）；
- **停止生成**：流式期间显示「■ 停止生成」按钮 → `AbortController` 中断 SSE，保留已生成内容并标注
  「已停止生成」，不触发非流式回退；
- **RAG 引用**：回答中的 `[K1]/[K2]` 渲染为可点击角标；底部「参考知识（N）」折叠区列出
  标题/来源/相关度/摘要；点击角标自动展开并滚动高亮对应引用条目；
- **自动回退**：`/api/chat/stream` 404/网络错误/中途失败 → 自动调用 `POST /api/chat` 非流式，
  回答顶部显示「⚠ 流式接口不可用…已自动切换普通模式」并 toast 提示；非流式回答同样支持 citations；
- SSE 事件契约（与 Agent 侧对齐）：`meta` / `status{stage,label}` / `tool{name,ok,elapsed_ms}` /
  `token{text}` / `action{action}` / `done{answer,trace,elapsed_ms,conversation_id}` / `error{message}`；
  RAG 命中在 `trace.retrieval.hits[{id,title,source,score}]`。

### W15-P2：可观测性页签（Agent 质量指标 / Trace / RAG 评测）

- 右侧信息坞新增第 5 个页签「可观测性」（`data-tab="obs"`），**懒加载**（仅打开时请求，不阻塞大屏）；
- **3 个指标卡**：平均响应延迟（ms/s · 调用次数）、工具错误率（% · 模型数）、Token 总量（k/M · 统计天数），
  数据来自 `GET /api/metrics/agent`；加载态显示「正在加载…」，失败时显示「—」并降级提示；
- **最近 Trace**：`GET /api/traces?limit=20`，按时间倒序展示时间 / 步骤 / 事件 / 工具 / 耗时 / ok；
  点击行展开事件序列（`GET /api/traces/{trace_id}`：每步 event/tool/耗时/tokens/错误）、
  汇总行展示总耗时/工具调用次数/tokens 输入输出；失败 trace 红色标识；支持「仅当前会话」过滤
  （勾选后用当前 AI 会话 `conversation_id` 过滤）；
- **RAG 评测卡**：`GET /api/rag/report` → recall@5 / MRR / 引用准确率 / faithfulness（百分比/MRR），
  显示评测样本数 tooltip；接口 404 时整个评测卡隐藏；
- 三个接口全部 404/不可用时页签显示「可观测性服务未就绪…不影响其它功能」，指标卡置「—」、
  trace 列表清空、RAG 卡隐藏；切换其它页签/3D 场景/AI 对话完全不受影响。

## 数据接入与真实性说明

| 页面内容 | 数据来源 |
|---|---|
| 网络设备标记 / 设备详情 | `GET /api/devices` |
| 主机（PC/Server）标记 | `GET /api/hosts`（含 `observed_at` / `snapshot_age_seconds`） |
| 设备间链路 | `GET /api/topology` 的 `links` |
| 告警列表 / 设备脉冲 | `GET /api/issues`（仅非 stale 的活跃问题参与脉冲）+ `GET /api/alerts?state=all`（含 `state/observed_at/source_ts/clear_reason`）+ `WS /ws` 实时告警与恢复摘要 |
| 活跃告警统计 | `GET /api/alerts/summary`（`/api/alerts` 响应内也带 summary） |
| 检测引擎信息 | `GET /api/detection/rules`（规则目录 + 生命周期统计） |
| 健康度 / 采集状态 | `GET /api/network-health`、`GET /api/health` |
| 实时增量 | `WS /ws`：首帧 `snapshot`，之后推送快照 / `alert` / `alerts_cleared` / `status` |
| NC 数据源控制器标记 | `GET /api/health` 的 `driver_status.reachable`（有 `GET /api/scene` 的 controller 条目时复用其位置） |
| W9 场景配置 | `GET /api/scene`、`GET /api/scene/pending`（4s 版本轮询） |
| W9 场景写操作 | `POST /api/scene/devices/{name}/confirm`、`DELETE /api/scene/devices/{name}`、`POST /api/scene/buildings/{id}/devices` |
| W9 告警管理 | `DELETE /api/alerts/{id}`、`POST /api/alerts/clear`、`POST /api/alerts/{id}/silence`、`POST /api/alerts/{id}/restore` |
| W9 审计 | `GET /api/audit`（web-ui 操作均带 `X-Operator`） |
| W9 会话历史 | `GET /api/conversations`、`GET /api/conversations/{id}`、`DELETE /api/conversations/{id}`、`POST /api/conversations/clear` |
| W9 Agent actions | `POST /api/actions/{id}/confirm`、`POST /api/actions/{id}/cancel` |
| W10 路径追踪 | `POST /api/path-trace {source_ip,dest_ip}`（NC flow-analysis，最多 30s 轮询；前端超时 40s） |
| W10 告警根因分组 | `GET /api/alerts/groups?state=active`（组/成员/suppressed_by） |
| W10 通知状态 | `GET /api/notify/status`、`POST /api/notify/test` |
| W12 流式问答 | `POST /api/chat/stream`（SSE：meta/status/tool/token/action/done/error；失败回退 `POST /api/chat`） |
| W12 RAG 引用 | `trace.retrieval.hits[{id,title,source,score}]` + 回答文本 `[K1]/[K2]` 标记 |
| W15 trace 查询 | `GET /api/traces?conversation_id=&limit=`、`GET /api/traces/{trace_id}`（Agent :8100） |
| W15 Agent 指标 | `GET /api/metrics/agent` → avg_latency_ms / tool_error_rate / tokens_total / calls / by_day / by_model |
| W15 RAG 评测 | `GET /api/rag/report` → recall@5 / MRR / 引用准确率 / faithfulness（404 时隐藏卡片） |
| AI 问答 | `POST /api/chat`（agent 服务 :8100，真实 DeepSeek 或 mock；响应含 actions[]） |

约束与保证：

1. **设备/主机/告警全部来自后端响应**，前端没有内置真实设备清单；`scripts/verify-devices.mjs`
   会双向比对「场景设备名 ↔ 后端设备名」，防止漏渲染或硬编码假设备。
2. 「NC 控制器」标记不是虚构设备，而是**数据源指示器**：仅当 `/api/health` 明确报告 NC 可达
   才出现在中心机房；NC 不可达时自动消失。
3. 楼栋归属规则（`src/scene/layout.js`）只决定设备画在哪栋楼，不决定设备是否存在。
4. **活跃语义以 `/api/alerts?state=active` 为准**：顶部活跃告警数、告警角标、3D 红色/脉冲、
   「活跃」标签只统计 active alerts（以及非 stale 的活跃 issue）。已恢复（cleared）告警灰显。
5. **NC 滞后 issue（`stale_source=true`）**：Network Controller 自身的问题记录清除可能滞后
   数分钟，后端已按宿主机实际状态（`ping_status` / `reachabilityStatus`）判定恢复并打上
   `stale_source=true`。前端将其显示为次要状态「已恢复 · NC 记录滞后」：**不计活跃、不红色、
   不脉冲**，时间用 `observed_at`，tooltip 说明“NC 问题记录滞后，数据源实际状态已恢复”。
6. **后端不可用时**：状态栏显示「未连接」，场景不展示任何设备，仅保留 4 栋楼体。
7. **演示数据**：默认关闭；仅当后端不可用且用户手动打开开关时加载内置 mock，界面持续显示
   「演示数据 · 非真实采集」水印，后端恢复后自动切回真实数据；关闭开关后演示设备立即清空。
8. **AI 回答仅作排障参考**：回答由大模型生成，页面固定提示“AI 生成内容仅供运维排障参考”。
9. **W9 场景配置来自后端**：楼栋/设备位置以 `GET /api/scene` 为准，前端仅保留默认布局作为接口不可用时的
   兜底；`pending` 新设备必须由用户「确认加入」后才算正式场景（未确认不参与活跃告警统计）。
10. **W9 写操作可审计、可撤销**：告警删除/清空/静默、场景确认/撤销均通过后端接口并携带
    `X-Operator: web-ui`；告警软删除支持 10 秒内撤销；前端不直接修改本地数据冒充成功。

### W7.1：stale_source 语义修复（NC 滞后 issue）

修复前：`/api/issues` 中 `stale_source=true` 的 PC-JX1 已恢复问题仍被前端当作活跃 issue，
导致顶部「活跃告警 0」与告警列表「PC-JX1 活跃」矛盾、3D 场景 PC-JX1 仍有红色脉冲
（见对比截图 [`docs/screenshot-stale-bug.png`](docs/screenshot-stale-bug.png)）。

修复方式：

- 新增纯函数模块 [`src/api/alertsView.js`](src/api/alertsView.js) 统一告警/问题的合并与状态语义，
  浏览器与 Node 冒烟脚本共用（可单测）；
- `src/scene/items.js` 的告警匹配只对非 stale 的活跃 issue 追加告警设备集合；
- 告警列表/详情：stale issue 显示「已恢复 · NC 记录滞后」，灰黄次要样式，tooltip 同时给出
  `observed_at`、`source_ts` 与 NC 滞后原因；底部统计额外显示 `NC 滞后 N`；
- 修复后效果见 [`docs/screenshot-recovered.png`](docs/screenshot-recovered.png)（PC-JX1 绿色、
  活跃告警 0、列表「已恢复 · NC 记录滞后」）。

## AI 真实调用实测记录

详见 [`docs/ai-call-record.md`](docs/ai-call-record.md)。摘要：

- 服务：`http://127.0.0.1:8100`，provider=`deepseek`，model=`deepseek-flash`，`provider_ready=true`；
- 首测：POST `/api/chat`，问题「现在校园网整体状态怎么样？有没有设备异常？」→ HTTP 200，
  **22.2 秒**返回 **5324 字符**中文 Markdown，4 步 / 10 次工具调用 / 0 错误；
- 前端冒烟实测：页面键盘输入「现在校园网整体状态怎么样？」→ 等待 **22.6 秒**，
  收到 **4783 字符**回答，渲染标题 4 个、列表项 25 个、代码块 1 个，trace 展开显示 11 条工具调用；
- Agent 离线降级：`VITE_AGENT_BASE` 指向不可达地址时，页面显示「AI 服务未连接」，
  3D 大屏/告警正常（见 `docs/screenshot-ai-offline.png`）。

## 自测

所有自测命令均在 `frontend/` 目录执行。

### 1. 构建

```powershell
npm run build
# 实测：✓ built，dist/assets/index-*.js ≈ 655 kB（gzip 170 kB）、css ≈ 22 kB
```

### 2. 设备一致性校验（真实后端）

前置：后端 `http://127.0.0.1:8000` 已启动。

```powershell
npm run verify:devices
```

复用浏览器端同一套 `normalize.js + items.js` 逻辑，双向校验 `/api/devices`、`/api/hosts`
与场景条目是否一致，打印设备明细表并写入 UTF-8 报告 `scripts/verify-report.txt`。

实测：**PASS —— 设备(5)/主机(6) 与后端完全一致，场景 12 个标记。**

### 3. 端到端冒烟（headless Edge + CDP，无需 Playwright）

脚本支持四种模式（`SMOKE_MODE`）。**live 模式会真实调用 DeepSeek（约 20~40s），请确保 8100 已启动。**
W9 新增 `w9` 模式：使用契约 mock（后端 8002 / Agent 8102）验证配置驱动场景、告警管理、
历史会话与 action 卡片；后端/Agent 的 W9 接口未就绪或不便操作真实数据时使用。

```powershell
# 模式 A：live（8000 + 8100 + 5173 全部就绪）
npm run smoke
```

覆盖：页面加载、4 楼 + 全部设备/主机标记、设备数与后端一致、可渲染对象 < 60、点击设备聚焦 +
详情面板（真实 IP/楼栋/数据观测时间）、空白取消选中、自动旋转开关、**W3 实时数据增强**
（数据新鲜度/observed_at/告警生命周期/检测引擎）、**W7.1 stale_source 语义**（纯函数单测 +
真实数据断言：stale issue 不活跃/不红色/不脉冲、列表显示「已恢复 · NC 记录滞后」、PC-JX1 绿色）、
**AI 真实问答**（输入 → 加载态 → 非空中文 Markdown 回答 → trace 展开）、
截图 `screenshot-overview/detail/chat/recovered.png`。

```powershell
# 模式 B：demo（后端离线 + 演示数据）
# 终端 A：$env:VITE_API_BASE='http://127.0.0.1:9'; npm run dev -- --port 5174
# 终端 B：
$env:SMOKE_MODE='demo'; $env:SMOKE_URL='http://127.0.0.1:5174/'; npm run smoke
```

覆盖：后端离线提示、默认不展示设备、手动开启演示数据、演示水印、2 台设备告警脉冲、关闭后清空，
截图 `screenshot-demo.png`。

```powershell
# 模式 C：ai-offline（后端正常、Agent 不可达）
# 终端 A：$env:VITE_AGENT_BASE='http://127.0.0.1:9'; npm run dev -- --port 5175
# 终端 B：
$env:SMOKE_MODE='ai-offline'; $env:SMOKE_URL='http://127.0.0.1:5175/'; npm run smoke
```

覆盖：「AI 服务未连接（地址）」提示、页签红点、发送/快捷提问禁用、重试按钮、3D 大屏与实时数据
不受影响，截图 `screenshot-ai-offline.png`。

```powershell
# 模式 D：w9（配置驱动场景 / 告警管理 / 会话 / action 卡片 / W10 路径追踪·分组·通知 / W12 流式·RAG，Mock 契约）
# 终端 A（Mock 后端 8002 + Mock Agent 8102）：
cd D:\zdsh\campus-twin\frontend
node scripts/mock-w9-server.mjs
# 终端 B（dev server 5176 指向 mock）：
$env:VITE_API_BASE='http://127.0.0.1:8002'; $env:VITE_AGENT_BASE='http://127.0.0.1:8102'
npm run dev -- --port 5176
# 终端 C：
npm run smoke:w9        # 等价于 SMOKE_MODE=w9 SMOKE_URL=http://127.0.0.1:5176/ npm run smoke
```

覆盖：动态楼栋渲染（5 栋，含新增「实验楼」）、pending 设备虚线框/角标/浅色、新设备自动出现
+3 秒高亮（无刷新）、新设备确认/撤销、告警删除 + 10 秒撤销恢复、活跃告警静默、清空已恢复、
Agent action 卡片（危险红色 + 取消 + 确认执行结果）、历史会话加载/删除/清空；
**W10**：路径追踪（完整路径 4 跳 / 断点路径 3 跳红色 ✕ / 清除）、告警根因分组（根因链路中断 +
被抑制成员灰显 + 分组内操作保留）、通知状态（已启用 + 测试发送 sent_count 递增）；
**W12**：SSE 事件顺序（meta→status→tool→token→done）、token 拼接与 done.answer 一致、停止生成
（AbortController + 保留已生成内容）、RAG 引用角标/参考知识折叠区/点击高亮、SSE 404 回退非流式并提示；
**W15**：可观测性页签（3 指标卡 / 最近 trace 列表与事件展开 / 仅当前会话过滤 / RAG 评测 4 指标 /
三接口 404 时指标「—」+ trace 清空 + RAG 隐藏 + 中文降级提示），
截图 `screenshot-w9-dynamic/alerts/actions.png`、`screenshot-w10-path/path-broken/groups.png`、
`screenshot-w12-streaming/rag.png`、`screenshot-w15-observability.png`。

实测结果（2026-10-30，Windows / Edge headless + CDP 真实鼠标键盘事件）：

| 用例 | 结果 | 关键数据 |
|---|---|---|
| `npm run build` | PASS | 28 modules，gzip JS 197.5 kB / CSS 9.2 kB |
| `npm run verify:devices` | PASS | 5 设备 + 6 主机与后端一致（NC 在线 12 标记；本次 NC 离线 11 标记，脚本按实时数据校验） |
| `npm run smoke`（live） | PASS | 首帧 ~0.7s；45 渲染对象；真实后端 `/api/scene` v3（4 栋/12 设备）配置驱动校验通过；W3/W7.1 断言通过（当前 NC 离线/采集陈旧，页面正确显示「后端采集陈旧」tooltip，断言兼容陈旧态）；AI 真实回答（Agent 流式接口未上线 → **W12 自动回退普通模式**，trace plan[] 正常）；live action 卡片（场景新增设备取消、危险链路仅取消，`/api/scene` v3 / pending=0 未变、无清空审计）；W10：通知状态与后端一致、NC 离线时 `/api/path-trace` 返回中文错误且前端正确降级（无 3D 残留/运行时错误）、根因分组空态正常；**W15：Agent 指标/trace/RAG 接口均未上线（404）→ 页签显示「可观测性服务未就绪」、指标卡「—」、trace 空、RAG 隐藏，无运行时错误** |
| `npm run smoke`（demo） | PASS | 12 标记、2 个告警脉冲、演示水印/离线提示正确 |
| `npm run smoke`（ai-offline） | PASS | 降级提示正确、AI 控件禁用、3D 场景 5+6 标记与后端一致 |
| `npm run smoke:w9` | PASS | 动态 5 栋楼 + pending 设备、自动发现高亮、确认/撤销、告警删除→撤销→恢复、静默、清空已恢复、2 个 action 卡片、历史会话加载/删除/清空；**W10**：路径 4 跳完整 + 3 跳断点（红 ✕）+ 清除、根因分组（链路中断/成员 3/被抑制 2/suppressed_by 灰显 + 操作保留）、通知已启用 + 测试发送；**W12**：流式状态条/3 个工具 chip/49 个 token chunk（290 字，与 done.answer 完全一致）/61 个 SSE 事件顺序正确、完成后 action+trace 不变、3 条 RAG 引用（6 个角标/点击高亮）、停止生成（94 字部分内容 + 「已停止生成」且无 done）、SSE 404 回退普通模式并提示；**W15**：指标卡（546 ms / 12.5 % / 26.6 k）、20 条 trace 与红色失败标识、展开 3 条事件、仅当前会话过滤、RAG 4 指标（recall@5 86% / MRR 0.74 / 引用准确率 91% / faithfulness 88%）、三接口 404 降级与恢复，全部通过 |
| 真实后端 UI 删除+撤销 | PASS | 通过页面删除 1 条已恢复告警并撤销：`/api/audit` 记录 `alert.delete` / `alert.restore`（operator=web-ui） |
| 真实 W10 联调 | PASS | 网络正常时 `/api/path-trace` 返回完整 3 跳并渲染 3D 发光路径；本次 NC 离线时返回中文错误（「NC 退避中…」），前端正确降级不残留路径；`/api/notify/status` 正常；`/api/alerts/groups` 空态正常 |
| 真实 W12 回退 | PASS | 真实 Agent `/api/chat/stream` 尚未上线（404）→ 自动回退 `/api/chat`，页面显示「⚠ 流式接口不可用…已自动切换普通模式」，回答/trace 正常，无运行时错误 |
| 真实 W15 降级 | PASS | Agent `/api/metrics/agent`、`/api/traces`、`/api/rag/report` 均未上线（404）→ 可观测性页签显示降级提示、指标卡「—」、trace 空、RAG 卡隐藏，无运行时错误；接口上线后前端自动展示（smoke live 已就绪分支）。测试中修正了 smoke 对「NC 离线/采集陈旧」与「路径追踪返回 ok:false」两种真实环境变化的兼容断言 |

> 当前后端真实数据恰好带 1 条 `stale_source=true` 的 PC-JX1 issue（NC 记录滞后、数据源已恢复），
> 冒烟以此做真实数据断言；若后端无 stale issue，该组断言会输出 `[INFO]` 跳过，不影响其余检查。

### 4. 截图说明

- `docs/screenshot-overview.png`：真实后端数据总览（4 栋楼、12 标记、状态栏、告警列表）。
- `docs/screenshot-detail.png`：点击「SW-SX-2960」后的聚焦视角 + 详情面板（真实 IP/型号/楼栋/
  数据观测时间/快照延迟）。
- `docs/screenshot-chat.png`：AI 运维助手真实 DeepSeek 问答（问题气泡 + Markdown 回答）。
- `docs/screenshot-recovered.png`：**W7.1 修复后**：PC-JX1 已恢复为绿色、无红色脉冲，顶部活跃告警 0，
  列表显示「已恢复 · NC 记录滞后」，详情面板「正常 / NC 滞后记录 1 条」。
- `docs/screenshot-stale-bug.png`：**W7.1 修复前对比**：同一时刻 PC-JX1 仍显示红色脉冲、
  列表「活跃」、角标 1，与顶部活跃告警 0 矛盾。
- `docs/screenshot-ai-offline.png`：Agent 不可达时的降级提示（红色状态、控件禁用、说明不影响大屏）。
- `docs/screenshot-w9-dynamic.png`：W9 配置驱动动态场景（5 栋楼、新增「实验楼」、pending 新设备虚线框/
  「新设备」角标 + 顶部「发现 N 台新设备」提示条）。
- `docs/screenshot-w9-alerts.png`：W9 告警管理（已恢复条目「删除」、活跃告警「静默」、
  顶部「清空已恢复（N）」、新设备提示条收起状态）。
- `docs/screenshot-w9-actions.png`：W9 Agent action 确认卡片（危险操作红色 + 危险标识 + 确认/取消 +
  执行结果「已清空 0 条已恢复告警」「操作已取消」）。
- `docs/screenshot-w9-live-actions.png`：W9.1 真实 Agent action 卡片（danger「清空已恢复告警」+
  「✓ 操作已取消」，未写入后端数据）。
- `docs/screenshot-w9-trace.png`：W9.1 多 Agent trace 展开（plan[] 子任务、工具序列、
  工具调用/错误数、每步耗时），兼容新 trace 结构。
- `docs/screenshot-w10-path.png`：W10 路径追踪（Mock 契约）：3D 发光路径 + 方向脉冲 +
  4 个 hop 编号/名称 + 面板 hop 列表（设备/角色/端口），完整路径状态。
- `docs/screenshot-w10-path-broken.png`：W10 断点路径：最后一段变红、红色 ✕ 断点标签/脉冲环、
  面板断点设备与 NC 错误信息。
- `docs/screenshot-w10-groups.png`：W10 告警根因分组：组卡片（链路中断 / 根因设备 Core-3650 /
  成员 3 / 被抑制 2）、根因成员标「根因」、被抑制成员灰显 +「被上游抑制」+ suppressed_by，
  分组内仍保留「静默」操作。
- `docs/screenshot-w10-path-live.png`：W10 真实后端联调：`POST /api/path-trace` 返回
  `PC-JX1 → Core-3650 → Server-1` 3 跳（1.0s / 缓存），3D 发光路径 + 真实端口/角色；
  顶部通知状态「已启用 / 通用 Webhook · 已发送 N 次」。
- `docs/screenshot-w12-streaming.png`：W12 流式渲染中：状态条「正在生成回答…」+ 工具 chip
  （get_health/get_devices/get_alerts）+ 逐 token 粗渲染内容 + 「停止生成」按钮 + 流式徽标。
- `docs/screenshot-w12-rag.png`：W12 RAG 引用：回答 `[K1]/[K2]` 可点击角标 +「参考知识（3）」
  折叠区（标题/来源/相关度/摘要）+ 点击角标高亮 K1 条目。
- `docs/screenshot-w15-observability.png`：W15 可观测性页签：3 个指标卡（546 ms / 12.5 % / 26.6 k）、
  最近 trace 列表（时间/事件/工具/耗时/成功失败标识）+ 第一条 trace 展开事件序列（tool_call 42ms /
  model_response 63ms / final_answer 31ms）+ RAG 评测卡（recall@5 86% / MRR 0.74 / 引用准确率 91% /
  faithfulness 88%）。
- `docs/screenshot-demo.png`：后端离线 + 演示数据模式（水印、离线提示、活跃/已恢复告警、脉冲光环）。

> 截图为 headless Edge 在 1600×900 窗口下的实际渲染结果（页面视口约 1570×805，无人工修图）。

## 性能说明

- 默认场景：设备/主机标记共 12 个，可渲染对象 45 个（场景节点 65 个，含 Group 容器）；
  W9 配置驱动场景会按后端配置动态增减楼栋/设备（如 5 栋楼 + pending 设备时约 56 个对象），
  冒烟断言使用动态上限而非写死 60；
- 同类设备共享几何体，告警光环全部通过 1 个 `InstancedMesh` 绘制，选中环全局复用 1 个；
- W10 路径追踪只在追踪期间增加约 17 个对象（光晕管/亮管/中心线/2 脉冲/hop 光柱与圆环/标签），
  清除路径后全部释放；路径几何体/贴图在重新追踪时完全 dispose，长期运行不累积；
- 无 LOD、无阴影贴图、无后处理；`renderer.setPixelRatio(min(dpr, 2))` 限制高分屏开销；
- 数据刷新时只在设备集合变化时增删对象，状态变化仅原地更新材质颜色，不重建场景；
- AI 流式 token 采用 80ms 节流「粗渲染」（仅处理标题/列表/加粗/行内代码），结束时一次性完整
  Markdown 渲染 + XSS 转义；停止生成使用 AbortController 立即中断 fetch 流；
- W15 可观测性页签懒加载（仅打开时请求 3 个接口），页面常驻 3D 渲染不受影响；trace 列表最多 20 行，
  展开详情按需加载单个 trace；接口 404 时只降级该页签，不产生额外轮询；
- AI 面板与 3D 大屏解耦：AI 请求在独立面板内异步执行，不影响渲染帧率与数据轮询；
  Agent 未启动时只做短超时（5s）健康检查失败，不会阻塞首屏。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 状态栏「后端连接：未连接」+ 顶部红色提示条 | 后端未启动或地址不对。确认 `http://127.0.0.1:8000/api/health` 可访问；检查 `VITE_API_BASE`。 |
| 连接正常但设备列表为空、采集状态「陈旧」 | NC 不在线（PT 未启动 / Real World Access 未开启）。后端会持续重试，NC 恢复后自动出数据。 |
| AI 页签显示「AI 服务未连接」 | agent 服务未启动或 `VITE_AGENT_BASE` 不对。检查 `http://127.0.0.1:8100/api/health`。 |
| AI 提问报 503 / provider 未就绪 | agent 服务已启动但未配置 `LLM_API_KEY`。配置 key，或 `LLM_PROVIDER=mock` 离线演示。 |
| AI 回答很慢 / 超时 | 真实 DeepSeek 单次 20~40s 属正常，前端超时 120s；超过 120s 提示超时，可重试。 |
| 页面可打开但 3D 区域空白 | 浏览器未启用 WebGL；页面会在顶部显示运行时错误。换用 Edge/Chrome 并确认硬件加速。 |
| 端口被占用 | Vite dev 会自动换端口；如需固定端口先释放 5173/4173，或加 `--port` 指定。 |
| WebSocket 连不上但页面仍有数据 | 前端会降级为 REST 轮询（5s），状态栏显示 `HTTP` 而非 `WS`；不影响数据正确性。 |
| 演示数据开关打开后无变化 | 该开关仅在后端离线时生效；后端在线时始终展示真实数据（会弹出提示）。 |
| 场景没有新增的楼栋/设备 | 确认 `GET /api/scene` 可访问（版本号）、后端自动发现是否已写入 pending；前端每 4s 轮询一次场景版本；顶部提示条出现后需人工「确认加入」。 |
| 告警「删除」按钮不出现 | 只有已恢复（cleared）的检测引擎告警可删除；活跃告警只能静默（后端不变式），NC 滞后 issue 由后端生命周期管理。 |
| 「清空已恢复」按钮置灰 | 当前没有已恢复告警（N=0）；删除/清空均为软删除并写审计，10 秒内可撤销单条删除。 |
| 历史会话显示「暂不支持」 | Agent 服务的会话持久化接口尚未就绪（404）；不影响 AI 问答，接口上线后自动可用。 |
| action 卡片执行失败/过期 | Agent action 10 分钟过期或已被执行；卡片会展示错误信息，可重新提问生成新的 action。 |
| 路径追踪提示「服务未就绪」 | 后端未部署 `/api/path-trace` 或 NC 不支持 flow-analysis；检查后端版本与 NC 连接，其它大屏功能不受影响。 |
| 路径追踪出现红色 ✕ 断点 | `trace_status=partial/failed`：NC 未能返回完整 L3 路径，红色 ✕ 是最后可达 hop，面板显示中文原因（目标不可达 / ARP 缺失等）。 |
| 告警分组视图为空 | 后端当前无活跃告警（`active=0`）；被抑制成员只做展示层灰显，不删除、不改变 active 状态。 |
| 通知显示「未配置」或无「测试」按钮 | 后端 `/api/notify/status` 未就绪或 `NOTIFY_ENABLED=false`；启用并配置 Webhook 后自动显示类型与测试按钮。 |
| AI 回答顶部显示「普通模式回退」 | Agent 的 `/api/chat/stream` 未上线或流式中断，前端自动回退非流式（实测无 404 时专属提示）；接口上线后自动走流式。 |
| 流式回答中途点「停止生成」 | 使用 AbortController 中断 SSE，保留已生成内容并标注「已停止生成」；不会回退重发请求。 |
| 回答没有「参考知识」区 | 本次回答未命中 RAG 知识库（`trace.retrieval.hits` 为空）；命中时 `[K1]/[K2]` 会变为可点击角标。 |
| 可观测性页签显示「服务未就绪」/指标卡「—」 | Agent 的 `/api/metrics/agent`、`/api/traces`、`/api/rag/report` 尚未上线（404）；仅该页签降级，其它功能正常，接口上线后点「刷新」即可。 |
| 可观测性 trace 列表为空 | `GET /api/traces` 无记录；勾选「仅当前会话」时若还没有 AI 会话会提示无法过滤；取消勾选可看全部。 |
| RAG 评测卡不显示 | `/api/rag/report` 未就绪/404 时按设计隐藏；该卡只在有评测报告时展示。 |
| 后端采集状态显示「陈旧」/路径追踪报 NC 错误 | NC（PT）离线或 Real World Access 关闭；后端会退避重试，恢复后告警/路径自动正常，前端已正确降级提示。 |

## 后续可扩展

- 基于 `/api/metrics/{device_id}` 的设备指标曲线与异常检测联动；
- AI 回答中的设备名/告警点击联动 3D 场景定位（当前已支持告警列表定位）；
- 多轮对话上下文与追问、AI 结论一键生成工单；
- 楼栋内部“机房 → 机柜 → 设备”三级下钻与告警时间轴回放。
