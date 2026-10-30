# 飞书机器人（Campus Twin Feishu Bot）

在飞书里直接问 AI / 查校园网络状态：**长连接模式，无需公网 IP、无需内网穿透**，本地启动即可用。

## 功能

| 消息 | 效果 |
|---|---|
| `状态` | 整体网络健康度（真实 NC 数据） |
| `告警` | 告警列表 + 根因分组 |
| `路径 PC-JX1 到 Server-1` | 真实 NC flow-analysis 路径追踪 |
| `读 configs/core-3650.txt` | 通过 MCP 读取真实配置文件 |
| `帮助` | 指令菜单 |
| 其他任意问题 | 原样交给 AI Agent（多 Agent 编排、RAG、流式回答） |
| `清空已恢复的告警` 等写操作 | **只返回待确认提示，不会自动执行**（需到 Web 大屏确认） |

## 架构

```
飞书群/私聊
   │  (长连接 WebSocket，无公网)
   ▼
feishu-bot (Node.js + @larksuiteoapi/node-sdk)
   │  HTTP POST /api/chat
   ▼
AI Agent :8100（LangGraph 多 Agent · 23 工具 · RAG）
   │
   ▼
后端 :8000（真实 NC 数据）/ MCP Bridge :8200
```

## 飞书开放平台配置（一次性，约 5 分钟）

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → 创建企业自建应用
2. 「添加应用能力」→ 开启 **机器人**
3. 「权限管理」→ 开通：
   - `im:message`（读取用户发给机器人的消息）
   - `im:message:send_as_bot`（以机器人身份发消息）
4. 「事件订阅」→ 选择 **使用长连接接收事件** → 添加事件 **接收消息 `im.message.receive_v1`**
5. 「凭证与基础信息」→ 复制 **App ID / App Secret**
6. 发布应用（版本发布 → 等待管理员审核；自建应用通常即时生效）
7. 把机器人拉进群，或私聊机器人

> 长连接模式不需要公网地址，本地 `node src/bot.mjs` 即可接收消息。

## 快速开始

```powershell
cd D:\zdsh\campus-twin\feishu-bot
copy .env.example .env        # 填入 FEISHU_APP_ID / FEISHU_APP_SECRET
npm install --registry=https://registry.npmmirror.com
npm start
```

看到 `长连接已启动` 后在飞书发消息即可。

## 本地开发（不连飞书）

```powershell
# 直接模拟一条消息，走完整处理链路并打印回复
node src/mock-run.mjs "状态"
node src/mock-run.mjs "路径 PC-JX1 到 Server-1"

# 离线自测（19 项，不需要飞书与真实 Agent）
node src/self-test.mjs
```

## 环境变量

| 变量 | 说明 |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭证（**仅本地 .env，不提交仓库**） |
| `AGENT_BASE` | Agent 地址，默认 `http://127.0.0.1:8100` |
| `BOT_ALLOWED_CHAT_IDS` | 允许的群 chat_id（逗号分隔；留空=所有群） |
| `BOT_ALLOWED_USER_IDS` | 允许的用户 open_id（可选） |
| `BOT_RATE_LIMIT_PER_MIN` | 每群每分钟请求上限，默认 10 |
| `BOT_MAX_REPLY_CHARS` | 单条消息最大字符，超出自动分条，默认 3800 |
| `AGENT_TIMEOUT_MS` | Agent 超时，默认 240000（AI 回答 20~40 秒） |

## 安全设计

- **凭证隔离**：App ID/Secret 只从 `.env`/环境变量读取，`.env` 已加入 `.gitignore`
- **白名单**：群/用户白名单，非白名单只回复拒绝提示
- **限流**：每群每分钟次数上限，防刷
- **写操作不执行**：机器人只返回「待确认」提示，写操作必须在 Web 大屏人工确认（与 Agent 的 `interrupt()` 一致）
- **去重**：同一 message_id 10 分钟内只处理一次（飞书 3 秒超时会重推）
- **日志**：只记录 chat/message 前缀与文本前 60 字，不打印密钥

## 目录结构

```
feishu-bot/
├── src/
│   ├── bot.mjs          # 主程序：长连接 + 事件分发
│   ├── handler.mjs      # 消息处理（去重/限流/路由/回复分条）
│   ├── router.mjs       # 指令路由（状态/告警/路径/读文件/写操作识别）
│   ├── agent-client.mjs # Agent HTTP 客户端（超时/会话保持）
│   ├── formatter.mjs    # Markdown→飞书文本、长文分条
│   ├── feishu.mjs       # 飞书发送器（SDK 封装 + 测试 fake）
│   ├── config.mjs       # 配置加载/白名单校验
│   ├── mock-run.mjs     # 本地模拟运行
│   └── self-test.mjs    # 离线自测（19 项）
├── .env.example
└── README.md
```

## 常见问题

| 问题 | 处理 |
|---|---|
| 启动报「缺少飞书应用配置」 | 复制 `.env.example` 为 `.env` 并填写 App ID/Secret |
| 飞书里发消息没反应 | 检查应用是否发布、机器人能力是否开启、事件订阅是否选择「长连接」、是否已加入群 |
| 回复很慢 | 正常：AI 会调用真实网络数据并推理，通常 20~40 秒；机器人会先回「正在分析…」 |
| 提示「当前群未在白名单」 | 在 `.env` 的 `BOT_ALLOWED_CHAT_IDS` 加上该群 chat_id，或留空允许所有群 |
| 不想让机器人碰写操作 | 已内置：写操作只提示，不执行 |
