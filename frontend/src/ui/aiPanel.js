/**
 * 「AI 运维助手」对话面板。
 *
 * 职责：
 * - 输入框 / 发送按钮 / Enter 发送 / 4 个快捷提问；
 * - 加载态（AI 正在分析… + 已等待秒数，真实模型 20~40s）；
 * - 回答渲染（极简 Markdown）+ 折叠的执行轨迹 trace（工具调用序列 / provider / model / 耗时）；
 * - Agent 服务 :8100 未启动时显示「AI 服务未连接」，只禁用 AI 面板，不影响 3D 大屏。
 *
 * 本面板只负责交互与展示，HTTP 调用在 api/agentClient.js。
 */
import { AGENT_LABEL, AgentClient } from '../api/agentClient.js';
import { confirmDialog } from './dialog.js';
import { escapeHtml, formatClock, formatDateTime } from './format.js';
import { renderMarkdown } from './markdown.js';

/** 快捷提问（对应任务要求的 4 个运维场景） */
const QUICK_QUESTIONS = [
  { label: '整体状态怎么样', question: '现在校园网整体状态怎么样？有没有设备异常？' },
  { label: '哪个设备掉线了', question: '现在哪个设备掉线了？请列出离线/异常设备及原因。' },
  { label: '教学楼连接不上', question: '教学楼连接不上，帮我排查原因。' },
  { label: '带宽是否拥塞', question: '当前带宽是否拥塞？有没有链路负载异常？' },
];

/** W9 Agent action 类型 → 中文标签（危险操作由后端 danger 字段控制红色样式） */
const ACTION_LABELS = {
  clear_cleared_alerts: '清空已恢复告警',
  delete_alert: '删除告警',
  silence_alert: '静默活跃告警',
  delete_conversation: '删除会话',
  clear_conversations: '清空会话历史',
  apply_scene_device: '写入新设备位置',
  apply_scene_building: '新增楼栋',
  confirm_scene_device: '确认新设备',
  remove_scene_device: '移除设备',
};

function formatDuration(seconds) {
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

export class AiPanel {
  constructor(
    root = document,
    { agent = new AgentClient(), onStatus = null, onToast = null, onDataChanged = null } = {},
  ) {
    this.root = root;
    this.agent = agent;
    this.onStatus = onStatus;
    this.onToast = onToast;
    this.onDataChanged = onDataChanged;
    this.status = { online: false, provider: null, model: null, checked: false, backend: null, error: '' };
    this._busy = false;
    this._ticker = null;
    this._statusRefreshing = false;
    this.conversationId = null;
    this._conversations = [];
    this._historyLoaded = false;
    this._historyAvailable = null;

    const byId = (id) => root.getElementById(id);
    this.el = {
      statusDot: byId('ai-status-dot'),
      statusText: byId('ai-status-text'),
      statusRetry: byId('ai-retry'),
      modelLine: byId('ai-model-line'),
      newChat: byId('ai-new'),
      historyButton: byId('ai-history'),
      historyPanel: byId('ai-history-panel'),
      historyList: byId('ai-history-list'),
      historyClose: byId('ai-history-close'),
      historyClear: byId('ai-history-clear'),
      quick: byId('ai-quick'),
      messages: byId('ai-messages'),
      form: byId('ai-form'),
      input: byId('ai-input'),
      send: byId('ai-send'),
      hint: byId('ai-hint'),
    };

    this._renderWelcome();
    this._renderQuickQuestions();
    this._bindEvents();
    this._refreshStatus();
    // 周期性探测 agent 状态（启动/关闭都能及时反映到面板）
    this._statusTimer = setInterval(() => this._refreshStatus(), 20000);
  }

  // ------------------------------------------------------------------ //
  // 初始化 / 事件
  // ------------------------------------------------------------------ //
  _renderWelcome() {
    if (!this.el.messages) return;
    this.el.messages.innerHTML = `
      <div class="ai-welcome" id="ai-welcome">
        <div class="ai-welcome-title"><span class="ai-spark">✦</span> AI 运维助手</div>
        <p>基于后端实时采集数据 + 大模型，输出<strong>根因分析 / 依据 / 排障步骤 / 建议命令</strong>。</p>
        <p class="ai-welcome-tip">点击上方快捷问题或直接输入；真实模型回答通常需要 20~40 秒，请耐心等待。</p>
      </div>`;
  }

  _renderQuickQuestions() {
    if (!this.el.quick) return;
    this.el.quick.innerHTML = QUICK_QUESTIONS.map(
      (item) =>
        `<button type="button" class="ai-quick-btn" data-question="${escapeHtml(
          item.question,
        )}" title="${escapeHtml(item.question)}">${escapeHtml(item.label)}</button>`,
    ).join('');
  }

  _bindEvents() {
    this.el.quick?.addEventListener('click', (event) => {
      const button = event.target.closest('.ai-quick-btn');
      if (!button) return;
      this.send(button.dataset.question || '');
    });

    this.el.form?.addEventListener('submit', (event) => {
      event.preventDefault();
      this.send(this.el.input?.value || '');
    });

    this.el.input?.addEventListener('keydown', (event) => {
      // Enter 发送；Shift+Enter 换行
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.send(this.el.input.value || '');
      }
    });

    this.el.newChat?.addEventListener('click', () => this.resetChat());
    this.el.statusRetry?.addEventListener('click', () => this._refreshStatus());

    // W9：历史会话入口
    this.el.historyButton?.addEventListener('click', () => this._toggleHistory());
    this.el.historyClose?.addEventListener('click', () => this._toggleHistory(false));
    this.el.historyClear?.addEventListener('click', () => this._clearConversations());
    this.el.historyList?.addEventListener('click', (event) => {
      const deleteButton = event.target.closest('button[data-conv-delete]');
      if (deleteButton) {
        event.stopPropagation();
        this._deleteConversation(deleteButton.dataset.convDelete);
        return;
      }
      const loadButton = event.target.closest('[data-conv-id]');
      if (loadButton) this._loadConversation(loadButton.dataset.convId);
    });

    // W9：AI 回答里的 action 确认卡片（确认执行 / 取消）
    this.el.messages?.addEventListener('click', (event) => {
      // W12：点击 [K1] 角标 → 展开/定位「参考知识」对应条目
      const citeButton = event.target.closest('.cite-ref');
      if (citeButton) {
        this._focusCitation(citeButton.dataset.cite);
        return;
      }
      const button = event.target.closest('button[data-action-role]');
      if (!button) return;
      const card = button.closest('.ai-action-card');
      const actionId = card?.dataset.actionId;
      if (!actionId) return;
      if (button.dataset.actionRole === 'confirm') this._runAction(actionId, 'confirm');
      else this._runAction(actionId, 'cancel');
    });
  }

  // ------------------------------------------------------------------ //
  // Agent 状态
  // ------------------------------------------------------------------ //
  async _refreshStatus() {
    if (this._statusRefreshing) return;
    this._statusRefreshing = true;
    if (!this.status.checked) this._setStatusText('正在检测 AI 服务…');
    try {
      const health = await this.agent.health();
      this.status = { ...health, checked: true };
    } catch (error) {
      this.status = {
        online: false,
        provider: null,
        model: null,
        checked: true,
        error: error?.message || String(error),
      };
    } finally {
      this._statusRefreshing = false;
      this._applyStatusView();
    }
  }

  _setStatusText(text) {
    if (this.el.statusText) this.el.statusText.textContent = text;
  }

  _applyStatusView() {
    const { online, provider, model, backend } = this.status;
    const { el } = this;
    if (el.statusDot) {
      el.statusDot.classList.toggle('ok', online);
      el.statusDot.classList.toggle('bad', !online);
    }
    if (online) {
      this._setStatusText(`${provider || 'AI'} · ${model || '模型'}`);
    } else {
      this._setStatusText(`AI 服务未连接（${AGENT_LABEL}）`);
    }
    if (el.modelLine) {
      el.modelLine.textContent = online
        ? `Agent ${AGENT_LABEL} · ${provider}/${model} · 后端数据 ${
            backend?.reachable === false ? '不可达' : '已连接'
          }`
        : `Agent ${AGENT_LABEL} · 未连接，AI 问答暂不可用（不影响 3D 大屏）`;
    }
    el.statusRetry?.classList.toggle('hidden', online);
    this._updateControls();
    this.onStatus?.({
      online,
      provider,
      model,
      label: AGENT_LABEL,
      backendReachable: backend?.reachable ?? null,
    });
  }

  _updateControls() {
    const disabled = !this.status.online || this._busy;
    if (this.el.send) {
      this.el.send.disabled = disabled;
      this.el.send.title = this.status.online
        ? this._busy
          ? 'AI 正在分析…'
          : '发送问题'
        : `AI 服务未连接（${AGENT_LABEL}），请先启动 agent 服务`;
    }
    for (const button of this.el.quick?.querySelectorAll('.ai-quick-btn') || []) {
      button.disabled = disabled;
    }
    if (this.el.input) {
      this.el.input.disabled = this._busy;
    }
    if (this.el.hint) {
      this.el.hint.textContent = this.status.online
        ? `AI 只读后端数据 · 单次回答通常 20~40 秒 · ${AGENT_LABEL}`
        : `AI 服务未连接（${AGENT_LABEL}）· 3D 大屏、告警等其它功能不受影响`;
    }
  }

  // ------------------------------------------------------------------ //
  // 对话
  // ------------------------------------------------------------------ //
  async send(question) {
    const text = String(question ?? '').trim();
    if (!text) {
      this.el.input?.focus();
      return;
    }
    if (this._busy) {
      this._toast('AI 正在分析上一条问题，请稍候', 'warn');
      return;
    }
    // 已知离线时先刷新一次状态：agent 可能刚刚启动
    if (!this.status.online) {
      await this._refreshStatus();
    }
    if (!this.status.online) {
      this._appendUserMessage(text);
      this._appendErrorMessage({
        message: `AI 服务未连接（${AGENT_LABEL}），请先启动 agent 服务后再提问。`,
        hint: '启动命令（另开终端）：cd campus-twin/agent 后运行 uvicorn app.api:app --port 8100；也可用 VITE_AGENT_BASE 指向其它地址。',
        kind: 'offline',
      });
      this._scrollToBottom();
      return;
    }

    this._busy = true;
    this._updateControls();
    this._appendUserMessage(text);
    if (this.el.input) this.el.input.value = '';
    // W12：优先流式；流式气泡先显示状态条，随后逐字渲染，完成后再渲染 citations/trace/actions
    const bubble = this._appendStreamingBubble();
    this._scrollToBottom();

    const startedAt = Date.now();
    const debug = {
      events: [],
      stages: [],
      tools: [],
      tokenChunks: 0,
      tokensJoined: '',
      stopped: false,
      fallback: false,
      fallbackAnswerLength: 0,
      citations: 0,
      actionEvents: 0,
      error: '',
    };
    this._lastStreamMetrics = debug;
    this._streamStopped = false;
    this._ticker = setInterval(() => {
      const timer = bubble.item.querySelector('.ai-stream-elapsed');
      if (timer) timer.textContent = `${((Date.now() - startedAt) / 1000).toFixed(1)}s`;
    }, 500);

    try {
      const result = await this.agent.chatStream(text, {
        conversationId: this.conversationId,
        onEvent: (event) => {
          debug.events.push(event.type);
          this._handleStreamEvent(event, bubble, debug);
        },
      });
      debug.tokensJoined = result.answer || debug.tokensJoined;
      debug.citations = Array.isArray(result.citations) ? result.citations.length : 0;
      if (result.conversationId) this.conversationId = result.conversationId;
      this._finalizeStreamingBubble(bubble, result);
      this.status.online = true;
      this._applyStatusView();
    } catch (error) {
      if (error?.kind === 'aborted' || this._streamStopped) {
        // 用户点击「停止生成」：保留已生成内容，标注已停止
        debug.stopped = true;
        bubble.flush?.();
        this._finalizeStreamingBubble(
          bubble,
          {
            answer: bubble.streamText || '（已停止生成）',
            trace: null,
            provider: bubble.provider,
            model: bubble.model,
            elapsedMs: Date.now() - startedAt,
            actions: [],
            citations: [],
          },
          { stopped: true },
        );
        this._toast('已停止生成', 'warn');
      } else {
        // W12：流式接口未就绪/中断失败 → 自动回退 /api/chat 非流式，并明确提示
        debug.fallback = true;
        debug.error = error?.message || String(error);
        bubble.setStatus('流式不可用，正在切换普通模式…');
        bubble.remove();
        const loading = this._appendLoading();
        this._scrollToBottom();
        try {
          const result = await this.agent.chat(text, { conversationId: this.conversationId });
          loading.remove();
          if (result.conversationId) this.conversationId = result.conversationId;
          debug.fallbackAnswerLength = String(result.answer || '').length;
          debug.citations = Array.isArray(result.citations) ? result.citations.length : 0;
          this._appendAnswer(
            { ...result, streamFallback: true },
            {
              fallbackNote: `流式接口不可用（${error?.message || '未知原因'}），已自动切换普通模式`,
            },
          );
          this._toast('流式接口不可用，已自动切换普通模式', 'warn');
          this.status.online = true;
          this._applyStatusView();
        } catch (fallbackError) {
          loading.remove();
          this._appendErrorMessage({
            message: fallbackError?.message || String(fallbackError),
            hint:
              fallbackError?.kind === 'offline'
                ? '请确认 agent 服务在 :8100 运行（GET /api/health 可访问）。'
                : '流式与普通问答均失败，详见错误信息；可稍后重试。',
            kind: fallbackError?.kind || 'unknown',
          });
          if (fallbackError?.kind === 'offline') {
            this.status.online = false;
            this._applyStatusView();
          }
          this._toast('AI 问答失败，详见对话中的错误提示', 'error');
        }
      }
    } finally {
      clearInterval(this._ticker);
      this._ticker = null;
      this._busy = false;
      this._streamStopped = false;
      this._updateControls();
      this._scrollToBottom();
    }
  }

  // ------------------------------------------------------------------ //
  // W12：流式渲染（SSE）+ RAG 引用
  // ------------------------------------------------------------------ //
  _appendStreamingBubble() {
    const item = document.createElement('div');
    item.className = 'ai-msg ai-msg-assistant ai-msg-streaming';
    item.innerHTML = `
      <div class="ai-answer-head">
        <span class="ai-answer-role">AI 分析 <span class="ai-stream-badge">流式</span></span>
        <span class="ai-answer-meta ai-stream-meta">正在连接…</span>
        <button type="button" class="ai-stream-stop" title="停止生成（AbortController 中断 SSE）">■ 停止生成</button>
      </div>
      <div class="ai-stream-status">
        <span class="ai-stream-dot" aria-hidden="true"></span>
        <span class="ai-stream-stage">正在连接 Agent…</span>
        <span class="ai-stream-elapsed">0.0s</span>
      </div>
      <div class="ai-stream-tools hidden"></div>
      <div class="ai-stream-placeholder">等待首个 token…</div>
      <div class="ai-markdown ai-stream-markdown"></div>`;
    this.el.messages.appendChild(item);

    const stageEl = item.querySelector('.ai-stream-stage');
    const metaEl = item.querySelector('.ai-stream-meta');
    const toolsEl = item.querySelector('.ai-stream-tools');
    const placeholderEl = item.querySelector('.ai-stream-placeholder');
    const markdownEl = item.querySelector('.ai-stream-markdown');
    let streamText = '';
    let pendingRender = null;
    let lastRenderAt = 0;
    const render = () => {
      pendingRender = null;
      lastRenderAt = performance.now();
      markdownEl.innerHTML = this._renderStreamLite(streamText);
      placeholderEl.classList.add('hidden');
      if (this._shouldAutoScroll()) this._scrollToBottom();
    };
    const scheduleRender = () => {
      const elapsed = performance.now() - lastRenderAt;
      if (elapsed >= 80) render();
      else if (!pendingRender) {
        pendingRender = setTimeout(render, Math.max(16, 80 - elapsed));
      }
    };
    const bubble = {
      item,
      provider: null,
      model: null,
      get streamText() {
        return streamText;
      },
      setMeta(provider, model) {
        this.provider = provider || this.provider;
        this.model = model || this.model;
        metaEl.textContent = `${this.provider || 'AI'} · ${this.model || '模型'} · 流式生成`;
      },
      setStage(label) {
        stageEl.textContent = label;
      },
      addTool(name, ok, elapsedMs) {
        toolsEl.classList.remove('hidden');
        const chip = document.createElement('span');
        chip.className = `ai-stream-tool${ok === false ? ' bad' : ''}`;
        chip.textContent = `⚙ ${name}${elapsedMs ? ` ${Math.round(Number(elapsedMs))}ms` : ''}${
          ok === false ? ' ✕' : ' ✓'
        }`;
        toolsEl.appendChild(chip);
      },
      appendToken(chunk) {
        streamText += String(chunk || '');
        scheduleRender();
      },
      flush() {
        if (pendingRender) {
          clearTimeout(pendingRender);
          pendingRender = null;
        }
        render();
      },
      setStatus(text) {
        stageEl.textContent = text;
      },
      remove() {
        if (pendingRender) clearTimeout(pendingRender);
        item.remove();
      },
    };
    item.querySelector('.ai-stream-stop')?.addEventListener('click', () => {
      this._streamStopped = true;
      bubble.setStatus('正在停止生成…');
      this.agent.cancelStream();
    });
    return bubble;
  }

  _streamStageLabel(stage) {
    return (
      {
        classify: '正在理解问题…',
        retrieve: '正在检索知识库…',
        retrieve_done: '知识库检索完成，整理引用…',
        tool: '正在调用工具…',
        aggregate: '正在生成回答…',
        generate: '正在生成回答…',
        polish: '正在润色回答…',
      }[stage] || `处理中（${stage}）…`
    );
  }

  _handleStreamEvent(event, bubble, debug) {
    switch (event.type) {
      case 'meta':
        if (event.conversation_id) debug.conversationId = event.conversation_id;
        bubble.setMeta(event.provider, event.model);
        break;
      case 'status':
        debug.stages.push({ stage: event.stage || '', label: event.label || '' });
        bubble.setStage(event.label || this._streamStageLabel(event.stage));
        break;
      case 'tool':
        debug.tools.push(event.name || 'tool');
        bubble.addTool(event.name || 'tool', event.ok, event.elapsed_ms);
        break;
      case 'token':
        debug.tokenChunks += 1;
        debug.tokensJoined += String(event.text || '');
        bubble.appendToken(event.text);
        break;
      case 'action':
        debug.actionEvents += 1;
        break;
      case 'done':
        debug.doneEvent = true;
        debug.doneAnswerLength = String(event.answer || '').length;
        bubble.setStage('生成完成，正在渲染引用与执行轨迹…');
        break;
      default:
        break;
    }
  }

  _finalizeStreamingBubble(bubble, result, { stopped = false } = {}) {
    bubble.flush?.();
    const item = bubble.item;
    item.className = `ai-msg ai-msg-assistant ai-msg-answer${stopped ? ' ai-msg-stopped' : ''}`;
    item.innerHTML = this._answerHtml(result, {
      extraNote: stopped ? '已停止生成：以上为已生成的部分内容' : '',
    });
    this._scrollToBottom();
  }

  /** 流式过程中的 Markdown「粗渲染」：轻量文本/标题/列表/加粗/行内代码，避免每 token 全量解析导致闪烁 */
  _renderStreamLite(text) {
    const escaped = escapeHtml(String(text || ''));
    if (!escaped.trim()) return '<span class="stream-muted">…</span>';
    return escaped
      .replace(/^#{1,4}\s+(.+)$/gm, '<div class="stream-line stream-heading">$1</div>')
      .replace(/^\s*[-*]\s+(.+)$/gm, '<div class="stream-line stream-bullet">• $1</div>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\n{2,}/g, '<div class="stream-gap"></div>')
      .replace(/\n/g, '<br />');
  }

  _shouldAutoScroll() {
    const container = this.el.messages;
    if (!container) return true;
    return container.scrollHeight - container.scrollTop - container.clientHeight < 180;
  }

  /** 回答正文 + RAG 引用 + action + trace 的统一 HTML（流式完成 / 非流式复用） */
  _answerHtml(
    {
      answer,
      trace,
      provider,
      model,
      elapsedMs,
      actions = [],
      citations = [],
      streamFallback = false,
    },
    { fallbackNote = '', extraNote = '' } = {},
  ) {
    let markdownHtml = '';
    try {
      markdownHtml = renderMarkdown(answer || '');
    } catch {
      markdownHtml = `<pre class="md-code">${escapeHtml(answer || '')}</pre>`;
    }
    markdownHtml = this._injectCitationBadges(markdownHtml, citations);
    const seconds = (Number(elapsedMs) || 0) / 1000;
    const meta = `${provider || 'AI'} · ${model || 'unknown'}${
      seconds ? ` · 耗时 ${formatDuration(seconds)}` : ''
    }${streamFallback ? ' · 普通模式回退' : ''}`;
    return `
      <div class="ai-answer-head">
        <span class="ai-answer-role">AI 分析</span>
        <span class="ai-answer-meta" title="provider / model / 本次调用耗时">${escapeHtml(meta)}</span>
        <time class="ai-answer-time">${escapeHtml(formatClock(Date.now() / 1000))}</time>
      </div>
      ${fallbackNote ? `<div class="ai-fallback-note">⚠ ${escapeHtml(fallbackNote)}</div>` : ''}
      ${extraNote ? `<div class="ai-stream-note">■ ${escapeHtml(extraNote)}</div>` : ''}
      <div class="ai-markdown">${markdownHtml}</div>
      ${this._renderCitations(citations)}
      ${this._renderActionCards(actions)}
      ${this._renderTrace(trace, { provider, model, elapsedMs })}
      <div class="ai-answer-foot">AI 生成内容仅供运维排障参考；危险操作需人工确认后执行。</div>`;
  }

  /** 把回答中的 [K1]/[K2] 渲染成可点击角标（仅渲染检索结果里存在的引用） */
  _injectCitationBadges(html, citations = []) {
    if (!Array.isArray(citations) || !citations.length) return html;
    const ids = new Set(citations.map((citation) => String(citation.id)));
    return String(html).replace(/\[K(\d+)\]/g, (match, index) => {
      const id = `K${index}`;
      if (!ids.has(id)) return match;
      return `<button type="button" class="cite-ref" data-cite="${id}" title="查看引用 ${id}">${id}</button>`;
    });
  }

  /** 底部「参考知识」折叠区（标题 / 来源 / 分数 / 摘要） */
  _renderCitations(citations = []) {
    if (!Array.isArray(citations) || !citations.length) return '';
    const items = citations
      .map((citation) => {
        const scoreText =
          citation.score !== null && citation.score !== undefined
            ? `相关度 ${(Number(citation.score) * 100).toFixed(0)}%`
            : '';
        const metaText = [citation.source, scoreText].filter(Boolean).join(' · ');
        return `<li class="citation-item" id="citation-${escapeHtml(
          String(citation.id),
        )}" data-cite="${escapeHtml(String(citation.id))}">
          <span class="citation-index">${escapeHtml(String(citation.id))}</span>
          <div class="citation-main">
            <div class="citation-title">${escapeHtml(citation.title || '未命名知识条目')}</div>
            ${
              metaText
                ? `<div class="citation-meta">${escapeHtml(metaText)}</div>`
                : ''
            }
            ${
              citation.snippet
                ? `<div class="citation-snippet">${escapeHtml(citation.snippet)}</div>`
                : ''
            }
          </div>
        </li>`;
      })
      .join('');
    return `<details class="ai-citations">
      <summary>参考知识 <span class="citation-count">${citations.length}</span></summary>
      <ol class="citation-list">${items}</ol>
    </details>`;
  }

  _focusCitation(id) {
    const container = this.el.messages;
    if (!container || !id) return;
    let target = null;
    for (const node of container.querySelectorAll('.citation-item')) {
      if (node.dataset.cite === String(id)) {
        target = node;
        break;
      }
    }
    if (!target) return;
    const details = target.closest('.ai-citations');
    if (details && !details.open) details.open = true;
    target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    target.classList.add('highlight');
    clearTimeout(this._citationHighlightTimer);
    this._citationHighlightTimer = setTimeout(() => target.classList.remove('highlight'), 1800);
  }

  /** 供 smoke / 控制台调试：最近一次流式请求的事件顺序、token、回退/停止状态 */
  getStreamDebug() {
    return this._lastStreamMetrics;
  }

  resetChat() {
    if (this._busy) {
      this._toast('AI 正在分析，请等待完成后再开始新对话', 'warn');
      return;
    }
    this.conversationId = null;
    this._toggleHistory(false);
    this._renderWelcome();
    this.el.input?.focus();
  }

  focusInput() {
    this.el.input?.focus();
  }

  isBusy() {
    return this._busy;
  }

  dispose() {
    clearInterval(this._statusTimer);
    clearInterval(this._ticker);
    this.agent.cancel();
    this.agent.cancelStream();
  }

  // ------------------------------------------------------------------ //
  // 消息渲染
  // ------------------------------------------------------------------ //
  _removeWelcome() {
    this.el.messages?.querySelector('#ai-welcome')?.remove();
  }

  _appendUserMessage(text) {
    this._removeWelcome();
    const item = document.createElement('div');
    item.className = 'ai-msg ai-msg-user';
    item.innerHTML = `<div class="ai-bubble">${escapeHtml(text).replace(/\n/g, '<br />')}</div>`;
    this.el.messages.appendChild(item);
  }

  _appendLoading() {
    const item = document.createElement('div');
    item.className = 'ai-msg ai-msg-assistant ai-msg-loading';
    item.innerHTML = `
      <div class="ai-loading">
        <span class="ai-spinner" aria-hidden="true"></span>
        <span class="ai-loading-text">AI 正在分析…</span>
        <span class="ai-wait-timer">已等待 0 秒</span>
      </div>
      <div class="ai-loading-hint">正在调用大模型并读取后端实时数据，通常需要 20~40 秒…</div>`;
    this.el.messages.appendChild(item);
    return item;
  }

  _appendErrorMessage({ message, hint, kind }) {
    const item = document.createElement('div');
    item.className = 'ai-msg ai-msg-assistant ai-msg-error';
    item.innerHTML = `
      <div class="ai-error-head"><span class="ai-error-icon">!</span><span>${escapeHtml(
        kind === 'offline' ? 'AI 服务未连接' : 'AI 分析失败',
      )}</span></div>
      <div class="ai-error-body">${escapeHtml(message || '未知错误')}</div>
      ${hint ? `<div class="ai-error-hint">${escapeHtml(hint)}</div>` : ''}`;
    this.el.messages.appendChild(item);
  }

  _appendAnswer(result, { fallbackNote = '' } = {}) {
    const item = document.createElement('div');
    item.className = 'ai-msg ai-msg-assistant ai-msg-answer';
    item.innerHTML = this._answerHtml(result, { fallbackNote });
    this.el.messages.appendChild(item);
    this._scrollToBottom();
  }

  // ------------------------------------------------------------------ //
  // W9：Agent action 确认卡片（危险操作红色 + 二次确认 + 执行结果）
  // ------------------------------------------------------------------ //
  _renderActionCards(actions = []) {
    const list = Array.isArray(actions) ? actions : [];
    if (!list.length) return '';
    return `<div class="ai-actions">${list
      .map((action) => {
        const id = escapeHtml(String(action.id ?? ''));
        const danger = Boolean(action.danger);
        // 新 Agent 直接给中文 label（如“场景新增设备”），旧接口按 type 映射兜底
        const typeLabel = action.label || ACTION_LABELS[action.type] || action.type || '操作';
        const params = action.params && Object.keys(action.params).length
          ? JSON.stringify(action.params)
          : '';
        return `<div class="ai-action-card${danger ? ' danger' : ''}" data-action-id="${id}">
          <div class="ai-action-head">
            <span class="ai-action-type">${danger ? '⚠ ' : '⚙ '}待确认操作：${escapeHtml(typeLabel)}</span>
            ${danger ? '<span class="ai-action-danger">危险操作</span>' : '<span class="ai-action-normal">需确认</span>'}
          </div>
          <div class="ai-action-confirm">${escapeHtml(action.confirm_text || '确认执行该操作？')}</div>
          ${params ? `<div class="ai-action-params" title="${escapeHtml(params)}">参数：${escapeHtml(params)}</div>` : ''}
          <div class="ai-action-buttons">
            <button type="button" class="btn small${danger ? ' danger' : ''}" data-action-role="confirm">确认执行</button>
            <button type="button" class="btn small ghost" data-action-role="cancel">取消</button>
          </div>
          <div class="ai-action-result hidden"></div>
        </div>`;
      })
      .join('')}</div>`;
  }

  _findActionCard(actionId) {
    for (const card of this.el.messages?.querySelectorAll('.ai-action-card') || []) {
      if (card.dataset.actionId === String(actionId)) return card;
    }
    return null;
  }

  async _runAction(actionId, mode) {
    const card = this._findActionCard(actionId);
    if (!card) return;
    const resultEl = card.querySelector('.ai-action-result');
    const buttons = card.querySelectorAll('button[data-action-role]');
    for (const button of buttons) button.disabled = true;
    card.classList.add('running');
    if (resultEl) {
      resultEl.classList.remove('hidden');
      resultEl.textContent = mode === 'confirm' ? '正在执行操作…' : '正在取消…';
    }
    try {
      const data =
        mode === 'confirm'
          ? await this.agent.confirmAction(actionId)
          : await this.agent.cancelAction(actionId);
      const ok = data?.ok !== false && !data?.error;
      card.classList.remove('running');
      card.classList.add(ok ? (mode === 'confirm' ? 'done' : 'cancelled') : 'failed');
      if (resultEl) {
        const message =
          data?.message ||
          data?.result?.message ||
          (typeof data?.result === 'string' ? data.result : '') ||
          (ok
            ? mode === 'confirm'
              ? '操作已执行成功'
              : '操作已取消'
            : data?.error || '操作失败');
        resultEl.textContent = `${ok ? '✓' : '✗'} ${message}`;
      }
      for (const button of buttons) {
        button.disabled = true;
        button.classList.add('hidden');
      }
      this._toast(
        mode === 'confirm' ? '操作已执行，场景/告警数据已刷新' : '操作已取消',
        ok ? 'info' : 'error',
      );
      if (ok && mode === 'confirm') this.onDataChanged?.();
      return;
    } catch (error) {
      card.classList.remove('running');
      card.classList.add('failed');
      if (resultEl) {
        resultEl.classList.remove('hidden');
        resultEl.textContent = `✗ ${error?.message || String(error)}`;
      }
      for (const button of buttons) button.disabled = false;
      this._toast('操作执行失败，详见卡片提示', 'error');
    }
  }

  // ------------------------------------------------------------------ //
  // W9：历史会话管理
  // ------------------------------------------------------------------ //
  async _toggleHistory(force) {
    const panel = this.el.historyPanel;
    if (!panel) return;
    const willOpen = typeof force === 'boolean' ? force : panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !willOpen);
    if (!willOpen) return;
    await this._loadConversations();
  }

  async _loadConversations() {
    const list = this.el.historyList;
    if (list) list.innerHTML = '<div class="ai-history-empty">正在加载会话…</div>';
    try {
      this._conversations = await this.agent.listConversations();
      this._historyAvailable = true;
    } catch (error) {
      this._historyAvailable = false;
      this._conversations = [];
      if (list) {
        list.innerHTML = `<div class="ai-history-empty">${
          error?.kind === 'unsupported'
            ? 'Agent 服务暂不支持会话历史（W9 功能未就绪）'
            : `加载会话失败：${escapeHtml(error?.message || String(error))}`
        }</div>`;
      }
      return;
    }
    this._renderHistoryList();
  }

  _renderHistoryList() {
    const list = this.el.historyList;
    if (!list) return;
    if (!this._conversations.length) {
      list.innerHTML = '<div class="ai-history-empty">暂无历史会话</div>';
      return;
    }
    list.innerHTML = this._conversations
      .map((conversation) => {
        const time = formatDateTime(conversation.updatedAt || conversation.createdAt);
        const count = conversation.messageCount || 0;
        return `<div class="ai-history-item${
          conversation.id === this.conversationId ? ' current' : ''
        }" data-conv-id="${escapeHtml(conversation.id)}" title="点击加载该会话">
          <div class="ai-history-item-main">
            <span class="ai-history-title">${escapeHtml(conversation.title || '未命名会话')}</span>
            <span class="ai-history-meta">${escapeHtml(time)} · ${count} 条消息</span>
          </div>
          <button type="button" class="ai-history-del" data-conv-delete="${escapeHtml(
            conversation.id,
          )}" title="删除该会话">删除</button>
        </div>`;
      })
      .join('');
  }

  async _loadConversation(id) {
    if (!id) return;
    try {
      const conversation = await this.agent.getConversation(id);
      this._removeWelcome();
      this.el.messages.innerHTML = '';
      this._appendHistoryMessages(conversation.messages || []);
      if (conversation.messages?.length) this.conversationId = conversation.id;
      this._toggleHistory(false);
      this._toast(`已加载历史会话「${conversation.title || id}」`);
    } catch (error) {
      this._toast(`加载会话失败：${error?.message || error}`, 'error');
    }
  }

  _appendHistoryMessages(messages = []) {
    for (const message of messages) {
      if (message.role === 'user') {
        this._appendUserMessage(message.content);
        continue;
      }
      const item = document.createElement('div');
      item.className = 'ai-msg ai-msg-assistant ai-msg-history';
      let markdownHtml = '';
      try {
        markdownHtml = renderMarkdown(message.content);
      } catch {
        markdownHtml = `<pre class="md-code">${escapeHtml(message.content)}</pre>`;
      }
      const actions = Array.isArray(message.meta?.actions) ? message.meta.actions : [];
      const citations = Array.isArray(message.meta?.citations) ? message.meta.citations : [];
      item.innerHTML = `
        <div class="ai-answer-head">
          <span class="ai-answer-role">AI 分析</span>
          <span class="ai-answer-meta">历史会话</span>
          <time class="ai-answer-time">${escapeHtml(formatDateTime(message.createdAt))}</time>
        </div>
        <div class="ai-markdown">${this._injectCitationBadges(markdownHtml, citations)}</div>
        ${this._renderCitations(citations)}
        ${this._renderActionCards(actions)}`;
      this.el.messages.appendChild(item);
    }
    this._scrollToBottom();
  }

  async _deleteConversation(id) {
    if (!id) return;
    const confirmed = await confirmDialog({
      title: '删除会话',
      message: `确认删除该历史会话？${
        id === this.conversationId ? '这是当前正在使用的会话，删除后将开始新对话。' : ''
      }`,
      confirmText: '删除',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await this.agent.deleteConversation(id);
      if (id === this.conversationId) {
        this.conversationId = null;
        this._renderWelcome();
      }
      this._toast('会话已删除');
      await this._loadConversations();
    } catch (error) {
      this._toast(`删除会话失败：${error?.message || error}`, 'error');
    }
  }

  async _clearConversations() {
    const confirmed = await confirmDialog({
      title: '清空全部历史会话',
      message: '确认清空全部历史会话？该操作不可恢复。',
      confirmText: '清空全部',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await this.agent.clearConversations();
      this.conversationId = null;
      this._renderWelcome();
      this._toast('历史会话已清空');
      await this._loadConversations();
    } catch (error) {
      this._toast(`清空会话失败：${error?.message || error}`, 'error');
    }
  }

  /**
   * 渲染执行轨迹（兼容三种后端 trace 结构）：
   * 1) W9 多 Agent：trace.plan[]（每个子任务 role/objective/tools_used/actions/elapsed_ms）
   *    + 顶层 tool_call_count / tool_sequence / stop_reason / polish；
   * 2) 单 Agent：trace.steps[]（每步 provider_elapsed_ms / tool_calls[] / finish_reason）；
   * 3) 旧版：只有 tool_sequence / tool_call_count。
   * 工具调用数优先用顶层 tool_call_count，缺失时按 steps[].tool_calls 或 plan[].tools_used 求和。
   */
  _renderTrace(trace, { provider, model, elapsedMs } = {}) {
    const safeTrace = trace && typeof trace === 'object' ? trace : {};
    const steps = Array.isArray(safeTrace.steps) ? safeTrace.steps : [];
    const plan = Array.isArray(safeTrace.plan) ? safeTrace.plan : [];
    const toolSequence = Array.isArray(safeTrace.tool_sequence) ? safeTrace.tool_sequence : [];
    const stepsCallCount = steps.reduce(
      (sum, step) => sum + (Array.isArray(step.tool_calls) ? step.tool_calls.length : 0),
      0,
    );
    const planCallCount = plan.reduce(
      (sum, item) => sum + (Array.isArray(item.tools_used) ? item.tools_used.length : 0),
      0,
    );
    const toolCallCount =
      Number(safeTrace.tool_call_count) || stepsCallCount || planCallCount || toolSequence.length;
    const toolErrorCount = Number(safeTrace.tool_error_count) || 0;
    const seconds = (Number(safeTrace.elapsed_ms) || Number(elapsedMs) || 0) / 1000;
    const toolSequenceHtml = toolSequence.length
      ? toolSequence.map((name) => `<code>${escapeHtml(name)}</code>`).join('<span class="trace-arrow">→</span>')
      : '<span class="trace-muted">（无工具调用）</span>';

    // 单 Agent trace：steps[]
    const stepsHtml = steps
      .map((step) => {
        const calls = Array.isArray(step.tool_calls) ? step.tool_calls : [];
        const callsHtml = calls.length
          ? calls
              .map((call) => {
                const args =
                  call.arguments && Object.keys(call.arguments).length
                    ? JSON.stringify(call.arguments)
                    : '';
                return `<li class="${call.ok ? 'ok' : 'err'}">
                  <code>${escapeHtml(call.name || 'tool')}(${escapeHtml(args)})</code>
                  <span>${escapeHtml(String(call.elapsed_ms ?? '?'))}ms · ${escapeHtml(
                    String(call.result_chars ?? 0),
                  )} chars${call.error ? ` · ${escapeHtml(String(call.error))}` : ''}</span>
                </li>`;
              })
              .join('')
          : '<li class="trace-muted">本步无工具调用</li>';
        return `<li>
          <div class="trace-step-head">第 ${escapeHtml(String(step.step ?? '?'))} 步 · 模型耗时 ${formatDuration(
            (Number(step.provider_elapsed_ms) || 0) / 1000,
          )}${step.finish_reason ? ` · ${escapeHtml(String(step.finish_reason))}` : ''}</div>
          <ul class="trace-calls">${callsHtml}</ul>
        </li>`;
      })
      .join('');

    // W9 多 Agent trace：plan[]（子 Agent 编排）；tools_used 渲染进 .trace-calls 便于统计
    const planHtml = plan
      .map((item, index) => {
        const tools = Array.isArray(item.tools_used) ? item.tools_used : [];
        const actionCount = Number(item.actions) || 0;
        const callsHtml = tools.length
          ? tools
              .map(
                (name) =>
                  `<li class="ok"><code>${escapeHtml(String(name))}</code><span>子 Agent 调用</span></li>`,
              )
              .join('')
          : '<li class="trace-muted">本子任务无工具调用</li>';
        return `<li class="trace-plan-item ${item.ok === false ? 'err' : 'ok'}">
          <div class="trace-step-head">${escapeHtml(
            String(item.label || item.role || `子任务 ${index + 1}`),
          )}${item.role ? ` <em class="trace-role">${escapeHtml(String(item.role))}</em>` : ''} · 耗时 ${formatDuration(
            (Number(item.elapsed_ms) || 0) / 1000,
          )}${actionCount ? ` · 生成 ${actionCount} 个 action` : ''}${
            item.ok === false ? ' · 失败' : ''
          }</div>
          ${item.objective ? `<div class="trace-plan-objective">${escapeHtml(String(item.objective))}</div>` : ''}
          ${item.error ? `<div class="trace-plan-error">${escapeHtml(String(item.error))}</div>` : ''}
          <ul class="trace-calls">${callsHtml}</ul>
        </li>`;
      })
      .join('');
    const planTitleHtml = plan.length
      ? `<div class="trace-plan-title">多 Agent 编排：${escapeHtml(
          String(safeTrace.mode || 'multi_agent'),
        )} · ${plan.length} 个子任务${
          Array.isArray(safeTrace.roles) && safeTrace.roles.length
            ? ` · 角色 ${safeTrace.roles.map((role) => escapeHtml(String(role))).join(' / ')}`
            : ''
        }</div>`
      : '';
    const stepItemsHtml =
      stepsHtml || planHtml || '<li class="trace-muted">trace 中无步骤信息</li>';

    const metaItems = [
      ['provider', safeTrace.provider || provider],
      ['model', safeTrace.model || model],
      ['后端数据', safeTrace.backend_mode || '—'],
      safeTrace.mode ? ['模式', safeTrace.mode] : null,
      ['子任务', `${plan.length || steps.length} ${plan.length ? '个' : '步'}`],
      ['停止原因', safeTrace.stop_reason || '—'],
      ['工具调用', `${toolCallCount} 次`],
      ['工具错误', `${toolErrorCount} 次`],
      Number(safeTrace.actions_count) ? ['生成 action', `${safeTrace.actions_count} 个`] : null,
      safeTrace.polish?.used
        ? ['润色耗时', formatDuration((Number(safeTrace.polish.elapsed_ms) || 0) / 1000)]
        : null,
    ]
      .filter(Boolean)
      .map(
        ([label, value]) =>
          `<span><em>${escapeHtml(label)}</em>${escapeHtml(String(value ?? '—'))}</span>`,
      )
      .join('');

    const stepCount = plan.length || steps.length;
    const summarySub = `${safeTrace.provider || provider || 'AI'} / ${
      safeTrace.model || model || '?'
    }${safeTrace.mode ? ` · ${safeTrace.mode}` : ''} · ${stepCount} ${
      plan.length ? '个子任务' : '步'
    } · ${toolCallCount} 次工具 · ${formatDuration(seconds)}`;

    return `<details class="ai-trace">
      <summary>
        <span class="trace-title">执行轨迹</span>
        <span class="trace-sub">${escapeHtml(summarySub)}</span>
        <span class="trace-toggle-hint">展开/收起</span>
      </summary>
      <div class="trace-body">
        <div class="trace-meta">${metaItems}</div>
        <div class="trace-sequence"><em>工具序列</em>${toolSequenceHtml}</div>
        ${planTitleHtml}
        <ol class="trace-steps">${stepItemsHtml}</ol>
      </div>
    </details>`;
  }

  _scrollToBottom() {
    if (!this.el.messages) return;
    this.el.messages.scrollTop = this.el.messages.scrollHeight;
  }

  _toast(message, type = 'info') {
    this.onToast?.(message, type);
  }
}
