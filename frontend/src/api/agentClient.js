/**
 * AI 运维 Agent 服务接入（默认 http://127.0.0.1:8100，可用 VITE_AGENT_BASE 覆盖）。
 *
 * 接口：
 * - GET  /api/health  服务/provider/后端数据源健康状态
 * - POST /api/chat    {question} → {answer, trace, provider, model, elapsed_ms}
 *
 * 真实 DeepSeek 一次问答通常 20~40 秒，因此默认超时 120 秒；
 * 与 3D 大屏解耦：agent 不可用时只影响 AI 面板，不影响大屏数据展示。
 */

const ENV_AGENT = import.meta.env?.VITE_AGENT_BASE;
/** Agent 服务地址（构建时可通过 VITE_AGENT_BASE 注入） */
export const AGENT_BASE = String(ENV_AGENT || 'http://127.0.0.1:8100').replace(/\/+$/, '');
/** 展示用短地址，如 127.0.0.1:8100 */
export const AGENT_LABEL = AGENT_BASE.replace(/^https?:\/\//, '');
/** 真实大模型问答超时（毫秒）：20~40s 属正常，留足 120s 余量 */
export const AGENT_TIMEOUT_MS = 120000;

async function fetchJson(url, { timeoutMs = 8000, ...options } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: 'no-store', ...options, signal: controller.signal });
    if (!response.ok) {
      let detail = '';
      try {
        const payload = await response.json();
        detail = typeof payload?.detail === 'string' ? payload.detail : '';
      } catch {
        /* 非 JSON 错误体忽略 */
      }
      const error = new Error(`HTTP ${response.status} ${response.statusText}`);
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/** 带类别（离线/超时/服务端错误）的 AI 调用错误，便于 UI 区分提示 */
export class AgentError extends Error {
  constructor(message, { kind = 'unknown', status = null, detail = '' } = {}) {
    super(message);
    this.name = 'AgentError';
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

/**
 * 从流式 done / 非流式响应中提取 RAG 引用（trace.retrieval.hits 或顶层 citations）。
 * 统一成 {id(K1..), title, source, score, snippet} 结构。
 */
export function extractCitations(payload = {}, trace = {}) {
  const hits = Array.isArray(payload?.citations)
    ? payload.citations
    : Array.isArray(trace?.retrieval?.hits)
      ? trace.retrieval.hits
      : [];
  return hits
    .map((hit, index) => ({
      id: String(hit.id || `K${index + 1}`),
      title: String(hit.title || hit.doc_title || ''),
      source: String(hit.source || hit.doc_source || ''),
      score: Number.isFinite(Number(hit.score)) ? Number(hit.score) : null,
      snippet: String(hit.snippet || hit.text || ''),
    }))
    .filter((hit) => hit.id && (hit.title || hit.snippet || hit.source));
}

export class AgentClient {
  constructor({ baseUrl = AGENT_BASE, timeoutMs = AGENT_TIMEOUT_MS } = {}) {
    this.baseUrl = String(baseUrl || AGENT_BASE).replace(/\/+$/, '');
    this.timeoutMs = timeoutMs;
    this.label = this.baseUrl.replace(/^https?:\/\//, '');
    this._chatController = null;
    this._streamController = null;
  }

  /** 健康检查（短超时，失败不抛异常，返回 online=false） */
  async health(timeoutMs = 5000) {
    try {
      const data = await fetchJson(`${this.baseUrl}/api/health`, { timeoutMs });
      return {
        online: true,
        provider: data.provider || 'unknown',
        model: data.model || 'unknown',
        providerReady: Boolean(data.provider_ready),
        providerError: data.provider_error || '',
        backend: data.backend || null,
        maxSteps: data.max_steps ?? null,
        service: data.service || 'campus-twin-agent',
        version: data.version || '',
      };
    } catch (error) {
      return {
        online: false,
        provider: null,
        model: null,
        providerReady: false,
        providerError: '',
        backend: null,
        error: error?.name === 'AbortError' ? '健康检查超时' : error?.message || String(error),
      };
    }
  }

  /**
   * 发起一次运维问答。
   * @param {string} question 自然语言问题
   * @param {object} options
   * @param {string|null} options.conversationId 继续已有会话（W9 会话持久化）
   * @returns {Promise<{answer:string, trace:object, provider:string, model:string,
   *   elapsedMs:number, conversationId:string|null, actions:Array}>}
   */
  async chat(question, { conversationId = null } = {}) {
    const startedAt = performance.now();
    const controller = new AbortController();
    this._chatController = controller;
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const payload = { question };
      if (conversationId) payload.conversation_id = conversationId;
      const data = await fetchJson(`${this.baseUrl}/api/chat`, {
        timeoutMs: this.timeoutMs,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!data || typeof data.answer !== 'string') {
        throw new AgentError('AI 服务返回格式异常（缺少 answer 字段）', { kind: 'invalid' });
      }
      return {
        answer: data.answer,
        trace: data.trace && typeof data.trace === 'object' ? data.trace : {},
        provider: data.provider || 'unknown',
        model: data.model || 'unknown',
        elapsedMs: Number(data.elapsed_ms) || Math.round(performance.now() - startedAt),
        conversationId: data.conversation_id || conversationId || null,
        actions: Array.isArray(data.actions) ? data.actions : [],
        // W12：非流式回退时同样展示 RAG 引用
        citations: extractCitations(data, data.trace),
      };
    } catch (error) {
      if (error instanceof AgentError) throw error;
      if (error?.name === 'AbortError') {
        throw new AgentError(
          `AI 分析超时（超过 ${Math.round(this.timeoutMs / 1000)} 秒），请稍后重试`,
          { kind: 'timeout' },
        );
      }
      if (error?.status === 503) {
        throw new AgentError(
          `AI 服务未就绪（provider 未配置）：${error.detail || '请在 agent 服务配置 LLM_API_KEY 或使用 mock 模式'}`,
          { kind: 'provider_not_ready', status: 503, detail: error.detail },
        );
      }
      if (error?.status) {
        throw new AgentError(
          `AI 服务返回错误（HTTP ${error.status}）${error.detail ? `：${error.detail}` : ''}`,
          { kind: 'http', status: error.status, detail: error.detail },
        );
      }
      throw new AgentError(`AI 服务未连接（${this.label}）：${error?.message || error}`, {
        kind: 'offline',
      });
    } finally {
      clearTimeout(timer);
      if (this._chatController === controller) this._chatController = null;
    }
  }

  /** 中断正在进行的问答请求（预留给“停止分析”按钮） */
  cancel() {
    try {
      this._chatController?.abort();
    } catch {
      /* 忽略 */
    }
  }

  /** 中断正在进行的流式问答（W12「停止生成」按钮） */
  cancelStream() {
    try {
      this._streamController?.abort();
    } catch {
      /* 忽略 */
    }
  }

  // ------------------------------------------------------------------ //
  // W12：流式问答（SSE） + RAG 引用
  // ------------------------------------------------------------------ //
  /**
   * POST /api/chat/stream（text/event-stream）逐事件回调。
   * 事件契约：meta / status{stage,label} / tool{name,ok,elapsed_ms} / token{text} /
   *          action{action} / done{answer,trace,elapsed_ms,conversation_id} / error{message}
   * @returns 与 chat() 同构的结果，额外包含 citations[] / events[] / streaming=true
   */
  async chatStream(question, { conversationId = null, onEvent = null } = {}) {
    const startedAt = performance.now();
    const controller = new AbortController();
    this._streamController = controller;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.timeoutMs);
    const events = [];
    try {
      const payload = { question };
      if (conversationId) payload.conversation_id = conversationId;
      let response;
      try {
        response = await fetch(`${this.baseUrl}/api/chat/stream`, {
          method: 'POST',
          cache: 'no-store',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
      } catch (error) {
        if (error?.name === 'AbortError') throw error;
        throw new AgentError(`AI 服务未连接（${this.label}）：${error?.message || error}`, {
          kind: 'offline',
        });
      }
      if (response.status === 404) {
        throw new AgentError('流式接口未就绪（HTTP 404）', { kind: 'unsupported', status: 404 });
      }
      if (!response.ok) {
        let detail = '';
        try {
          const body = await response.json();
          detail = typeof body?.detail === 'string' ? body.detail : '';
        } catch {
          /* 非 JSON 错误体忽略 */
        }
        const httpError = new Error(`HTTP ${response.status} ${response.statusText}`);
        httpError.status = response.status;
        httpError.detail = detail;
        throw httpError;
      }
      if (!response.body) {
        throw new AgentError('当前浏览器不支持流式响应', { kind: 'unsupported' });
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let doneEvent = null;
      let streamError = null;
      const handleFrame = (frame) => {
        const dataLines = frame
          .split(/\r?\n/)
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart());
        const data = dataLines.join('\n').trim();
        if (!data || data === '[DONE]') return;
        let event;
        try {
          event = JSON.parse(data);
        } catch {
          return;
        }
        if (!event || typeof event !== 'object') return;
        events.push(event);
        try {
          onEvent?.(event);
        } catch {
          /* 回调异常不影响流读取 */
        }
        if (event.type === 'done') doneEvent = event;
        if (event.type === 'error') {
          streamError = new AgentError(event.message || '流式生成失败', { kind: 'stream_error' });
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let separatorIndex = buffer.search(/\r?\n\r?\n/);
        while (separatorIndex >= 0) {
          const frame = buffer.slice(0, separatorIndex);
          buffer = buffer.slice(separatorIndex).replace(/^\r?\n\r?\n/, '');
          handleFrame(frame);
          separatorIndex = buffer.search(/\r?\n\r?\n/);
        }
        if (streamError) break;
      }
      if (buffer.trim()) handleFrame(buffer);
      if (streamError) throw streamError;
      if (!doneEvent) {
        throw new AgentError('流式响应异常结束（未收到 done 事件）', { kind: 'stream_error' });
      }

      const metaEvent = events.find((event) => event.type === 'meta') || {};
      const tokenText = events
        .filter((event) => event.type === 'token' && typeof event.text === 'string')
        .map((event) => event.text)
        .join('');
      const answer =
        typeof doneEvent.answer === 'string' && doneEvent.answer.length
          ? doneEvent.answer
          : tokenText;
      const streamActions = events
        .filter((event) => event.type === 'action' && event.action)
        .map((event) => event.action);
      const trace = doneEvent.trace && typeof doneEvent.trace === 'object' ? doneEvent.trace : {};
      return {
        answer,
        trace,
        provider: doneEvent.provider || metaEvent.provider || 'unknown',
        model: doneEvent.model || metaEvent.model || 'unknown',
        elapsedMs: Number(doneEvent.elapsed_ms) || Math.round(performance.now() - startedAt),
        conversationId:
          doneEvent.conversation_id || metaEvent.conversation_id || conversationId || null,
        actions:
          Array.isArray(doneEvent.actions) && doneEvent.actions.length
            ? doneEvent.actions
            : streamActions,
        citations: extractCitations(doneEvent, trace),
        events,
        streaming: true,
      };
    } catch (error) {
      if (error instanceof AgentError) throw error;
      if (error?.name === 'AbortError') {
        if (timedOut) {
          throw new AgentError(
            `AI 分析超时（超过 ${Math.round(this.timeoutMs / 1000)} 秒），请稍后重试`,
            { kind: 'timeout' },
          );
        }
        throw new AgentError('已停止生成', { kind: 'aborted' });
      }
      if (error?.status === 503) {
        throw new AgentError('AI 服务未就绪（provider 未配置）', {
          kind: 'provider_not_ready',
          status: 503,
          detail: error.detail,
        });
      }
      if (error?.status) {
        throw new AgentError(`流式服务返回错误（HTTP ${error.status}）`, {
          kind: 'http',
          status: error.status,
          detail: error.detail,
        });
      }
      throw new AgentError(`AI 流式服务异常（${this.label}）：${error?.message || error}`, {
        kind: 'stream_error',
      });
    } finally {
      clearTimeout(timer);
      if (this._streamController === controller) this._streamController = null;
    }
  }

  // ------------------------------------------------------------------ //
  // W9：会话历史 & Agent action 确认
  // ------------------------------------------------------------------ //
  async _request(path, { method = 'GET', body = null, timeoutMs = 8000 } = {}) {
    const options = { method, timeoutMs };
    if (body !== null && body !== undefined) {
      options.headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
      options.body = JSON.stringify(body);
    }
    try {
      return await fetchJson(`${this.baseUrl}${path}`, options);
    } catch (error) {
      if (error?.status === 404) {
        throw new AgentError('Agent 服务暂不支持该接口（W9 功能未就绪）', {
          kind: 'unsupported',
          status: 404,
        });
      }
      if (error?.name === 'AbortError') {
        throw new AgentError('请求超时', { kind: 'timeout' });
      }
      throw new AgentError(
        `Agent 服务请求失败${error?.detail ? `：${error.detail}` : `：${error?.message || error}`}`,
        { kind: error?.status ? 'http' : 'offline', status: error?.status || null },
      );
    }
  }

  /** 会话列表：{ conversations:[{id,title,createdAt,updatedAt,messageCount}] } */
  async listConversations(timeoutMs = 5000) {
    const data = await this._request('/api/conversations', { timeoutMs });
    const raw = Array.isArray(data) ? data : data?.conversations || data?.items || [];
    return raw.map((item) => ({
      id: String(item.id ?? item.conversation_id ?? ''),
      title: String(item.title || '未命名会话'),
      createdAt: item.created_at ?? item.createdAt ?? null,
      updatedAt: item.updated_at ?? item.updatedAt ?? null,
      messageCount: Number(item.message_count ?? item.messageCount ?? item.messages?.length ?? 0),
    })).filter((item) => item.id);
  }

  /** 单个会话详情：返回 { id, title, messages:[{role,content,meta,createdAt}] } */
  async getConversation(id, timeoutMs = 8000) {
    const data = await this._request(`/api/conversations/${encodeURIComponent(id)}`, { timeoutMs });
    const conversation = data?.conversation || data || {};
    const rawMessages = Array.isArray(data?.messages)
      ? data.messages
      : Array.isArray(conversation.messages)
        ? conversation.messages
        : [];
    return {
      id: String(conversation.id ?? conversation.conversation_id ?? id),
      title: String(conversation.title || '历史会话'),
      messages: rawMessages.map((message) => ({
        role: String(message.role || 'assistant'),
        content: String(message.content ?? message.text ?? ''),
        meta: message.meta && typeof message.meta === 'object' ? message.meta : {},
        createdAt: message.created_at ?? message.createdAt ?? null,
      })),
    };
  }

  async deleteConversation(id) {
    return this._request(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }

  async clearConversations() {
    return this._request('/api/conversations/clear', { method: 'POST', body: {} });
  }

  /** 确认执行 Agent 提议的危险/写操作（action） */
  async confirmAction(id, timeoutMs = 20000) {
    return this._request(`/api/actions/${encodeURIComponent(id)}/confirm`, {
      method: 'POST',
      body: {},
      timeoutMs,
    });
  }

  async cancelAction(id) {
    return this._request(`/api/actions/${encodeURIComponent(id)}/cancel`, {
      method: 'POST',
      body: {},
    });
  }
}
