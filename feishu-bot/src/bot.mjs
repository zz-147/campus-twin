// -*- mode: js -*-
/**
 * 飞书机器人主程序（长连接模式，无需公网 IP）。
 *
 * 启动：node src/bot.mjs
 * 前置：.env 填 FEISHU_APP_ID / FEISHU_APP_SECRET；
 *       飞书开放平台配置：自建应用 → 开启机器人 → 订阅事件（长连接）→ 权限 im:message
 */
import * as Lark from '@larksuiteoapi/node-sdk';
import { config, assertLiveConfig, assertAllowed } from './config.mjs';
import { createHandler } from './handler.mjs';
import { createFeishuSender } from './feishu.mjs';

function log(level, ...args) {
  const levels = { debug: 10, info: 20, warn: 30, error: 40 };
  if ((levels[level] || 20) >= (levels[config.logLevel] || 20)) {
    const line = `[${new Date().toISOString()}] [${level.toUpperCase()}]`;
    if (level === 'error') console.error(line, ...args);
    else console.log(line, ...args);
  }
}

async function main() {
  assertLiveConfig(config);
  log('info', `飞书机器人启动中：Agent=${config.agentBase}`);
  log('info', `白名单：群=${config.allowedChatIds.length} 个，用户=${config.allowedUserIds.length} 个；频率上限=${config.rateLimitPerMin}/分钟`);

  const handler = createHandler({ cfg: config });
  const sender = createFeishuSender(config);

  const eventDispatcher = new Lark.EventDispatcher({}).register({
    'im.message.receive_v1': async (data) => {
      const message = data?.message || {};
      const chatId = message.chat_id || '';
      const messageId = message.message_id || '';
      const senderId = data?.sender?.sender_id?.open_id || data?.sender?.sender_id?.user_id || '';
      let text = '';
      try {
        text = JSON.parse(message.content || '{}').text || '';
      } catch {
        text = '';
      }
      // 过滤机器人自己/空消息
      if (data?.sender?.sender_type === 'app' || !text.trim()) return;

      const denied = assertAllowed(config, { chatId, userId: senderId });
      if (denied) {
        log('warn', '拒绝消息：', denied.slice(0, 80));
        await sender.sendText(chatId, denied).catch((e) => log('error', '发送拒绝提示失败:', e.message));
        return;
      }

      log('info', `收到消息 chat=${chatId.slice(0, 12)}… msg=${messageId.slice(0, 12)}… text=${text.slice(0, 60)}`);
      // 3 秒内 ack，避免飞书重推；随后异步处理
      sender.sendText(chatId, '收到，正在查询真实网络数据并分析，约 20~40 秒…').catch(() => {});
      try {
        const replies = await handler.handle({ chatId, userId: senderId, messageId, text });
        for (const reply of replies) {
          await sender.sendText(chatId, reply);
        }
        log('info', `回复完成：${replies.length} 条`);
      } catch (error) {
        log('error', '处理消息失败：', error?.message || error);
        await sender
          .sendText(chatId, `⚠ 处理失败：${error?.message || error}\n请稍后重试，或在 Web 大屏使用 AI 面板。`)
          .catch(() => {});
      }
    },
  });

  const wsClient = new Lark.WSClient({
    appId: config.appId,
    appSecret: config.appSecret,
    loggerLevel: Lark.LoggerLevel.info,
  });

  wsClient.start({ eventDispatcher });
  log('info', '长连接已启动：在飞书里给机器人发消息即可（Ctrl+C 退出）');

  process.on('SIGINT', () => {
    log('info', '收到退出信号，关闭机器人');
    process.exit(0);
  });
}

main().catch((error) => {
  console.error('启动失败：', error?.message || error);
  process.exit(1);
});
