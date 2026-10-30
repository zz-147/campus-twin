// -*- mode: js -*-
/** 调用校园网络数字孪生平台 Agent（POST /api/chat），带超时、错误归一化、会话保持。 */

export function createAgentClient(cfg, { fetchImpl = globalThis.fetch } = {}) {
  const conversations = new Map(); // chatId -> conversation_id（多轮会话）

  async function chat(question, { chatId = '', timeoutMs = cfg.agentTimeoutMs } = {}) {
    const started = Date.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const body = { question };
      const conv = conversations.get(chatId);
      if (conv) body.conversation_id = conv;
      const resp = await fetchImpl(`${cfg.agentBase}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await resp.text();
      if (!resp.ok) {
        return { ok: false, error: `Agent 返回 HTTP ${resp.status}：${text.slice(0, 200)}`, elapsedMs: Date.now() - started };
      }
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        return { ok: false, error: 'Agent 返回非 JSON 响应', elapsedMs: Date.now() - started };
      }
      if (data.conversation_id) conversations.set(chatId, data.conversation_id);
      return {
        ok: true,
        answer: String(data.answer || '').trim(),
        actions: Array.isArray(data.actions) ? data.actions : [],
        trace: data.trace || null,
        provider: data.provider,
        model: data.model,
        conversationId: data.conversation_id || '',
        elapsedMs: data.elapsed_ms || Date.now() - started,
      };
    } catch (error) {
      const aborted = error?.name === 'AbortError';
      return {
        ok: false,
        error: aborted
          ? `Agent 响应超时（${Math.round(timeoutMs / 1000)} 秒）`
          : `无法连接 Agent（${cfg.agentBase}）：${error?.message || error}`,
        elapsedMs: Date.now() - started,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  return { chat, resetConversation: (chatId) => conversations.delete(chatId) };
}
