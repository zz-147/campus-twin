// -*- mode: js -*-
/** 消息处理核心：可注入 agentClient / config，便于离线自测。 */
import { createAgentClient } from './agent-client.mjs';
import { routeMessage, formatActionNotice } from './router.mjs';
import { buildReply, splitText } from './formatter.mjs';

export function createHandler({ cfg, agentClient = createAgentClient(cfg) }) {
  const dedupe = new Map(); // messageId -> timestamp
  const rate = new Map();   // chatId -> {windowStart, count}

  function isDuplicate(messageId) {
    if (!messageId) return false;
    const now = Date.now();
    for (const [key, ts] of dedupe) {
      if (now - ts > 10 * 60 * 1000) dedupe.delete(key);
    }
    if (dedupe.has(messageId)) return true;
    dedupe.set(messageId, now);
    return false;
  }

  function rateLimited(chatId) {
    const now = Date.now();
    const item = rate.get(chatId);
    if (!item || now - item.windowStart > 60_000) {
      rate.set(chatId, { windowStart: now, count: 1 });
      return false;
    }
    item.count += 1;
    return item.count > cfg.rateLimitPerMin;
  }

  /**
   * @returns {Promise<string[]>} 需要发送给飞书的文本列表（可能分多条）
   */
  async function handle({ chatId, userId = '', messageId = '', text = '' }) {
    if (isDuplicate(messageId)) return [];
    if (rateLimited(chatId)) {
      return [`⚠ 请求太频繁（每分钟上限 ${cfg.rateLimitPerMin} 次），请稍后再试。`];
    }
    const routed = routeMessage(text);
    if (routed.kind === 'empty' || routed.kind === 'help') {
      return [routed.reply];
    }

    const result = await agentClient.chat(routed.question, { chatId });
    const footer = result.ok
      ? `\n— 校园网络数字孪生平台 · ${result.provider || 'AI'}/${result.model || 'model'} · ${Math.round((result.elapsedMs || 0) / 1000)}s`
      : '';
    let reply = buildReply({ result, routed, footer });
    if (routed.needsConfirm) {
      reply += '';
    }
    if (result.ok && result.actions?.length) {
      reply = reply.replace('⚠ 写操作需人工确认：请到 Web 大屏「AI 面板」点确认执行，机器人不会自动执行。', formatActionNotice(result.actions).trim());
    }
    return splitText(reply, cfg.maxReplyChars);
  }

  return { handle, isDuplicate, _state: { dedupe, rate } };
}
