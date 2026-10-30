/**
 * W15-P2 可观测性面板（右侧信息坞「可观测性」页签）。
 *
 * 数据源（Agent :8100，由 agent-dev 并行实现，未就绪时优雅降级）：
 * - GET /api/metrics/agent → {avg_latency_ms, tool_error_rate, tokens_total, calls, by_day[], by_model[]}
 * - GET /api/traces?conversation_id=&limit= → {count, items:[{trace_id, conversation_id, step, event,
 *   tool, provider, model, tokens_in, tokens_out, latency_ms, ok, error, ts}]}
 * - GET /api/traces/{trace_id} → 单条 trace 汇总（steps / 工具调用 / tokens / 耗时）
 * - GET /api/rag/report → recall@5 / MRR / 引用准确率 / faithfulness（404 → 隐藏该卡片）
 *
 * 本模块只负责 DOM/交互；接口调用与错误分类由 main.js 编排（onLoad / onLoadTrace）。
 */
import { escapeHtml, formatClock } from './format.js';

function num(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** epoch 秒/毫秒/ISO 字符串 → 毫秒时间戳 */
function toMs(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
  const parsed = Date.parse(String(value));
  return Number.isFinite(parsed) ? parsed : null;
}

function formatLatency(ms) {
  const value = num(ms);
  if (value === null) return '—';
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${Math.round(value)} ms`;
}

function formatRate(value) {
  const parsed = num(value);
  if (parsed === null) return '—';
  const percent = parsed <= 1 ? parsed * 100 : parsed;
  return `${percent.toFixed(1)} %`;
}

function formatTokens(value) {
  const parsed = num(value);
  if (parsed === null) return '—';
  if (parsed >= 1000000) return `${(parsed / 1000000).toFixed(2)} M`;
  if (parsed >= 10000) return `${(parsed / 1000).toFixed(1)} k`;
  return String(Math.round(parsed));
}

/** RAG 报告字段兼容（recall_at_5 / recall@5 / metrics.recall@5 等） */
function pickRagValue(raw, keys) {
  for (const source of [raw, raw?.metrics, raw?.report, raw?.data]) {
    if (!source || typeof source !== 'object') continue;
    for (const key of keys) {
      if (source[key] !== undefined && source[key] !== null) return source[key];
    }
  }
  return null;
}

function normalizeMetrics(raw) {
  if (!raw || typeof raw !== 'object') return null;
  return {
    avgLatencyMs: num(raw.avg_latency_ms ?? raw.avgLatencyMs),
    toolErrorRate: num(raw.tool_error_rate ?? raw.toolErrorRate),
    tokensTotal: num(raw.tokens_total ?? raw.tokensTotal),
    calls: num(raw.calls ?? raw.call_count ?? raw.calls_total),
    byDay: Array.isArray(raw.by_day) ? raw.by_day : [],
    byModel: Array.isArray(raw.by_model) ? raw.by_model : [],
  };
}

function normalizeTraceItem(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const traceId = String(raw.trace_id ?? raw.traceId ?? '');
  if (!traceId) return null;
  return {
    traceId,
    conversationId: String(raw.conversation_id ?? raw.conversationId ?? ''),
    step: num(raw.step),
    event: String(raw.event ?? raw.event_type ?? ''),
    tool: String(raw.tool ?? raw.tool_name ?? ''),
    provider: String(raw.provider ?? ''),
    model: String(raw.model ?? ''),
    tokensIn: num(raw.tokens_in ?? raw.tokensIn),
    tokensOut: num(raw.tokens_out ?? raw.tokensOut),
    latencyMs: num(raw.latency_ms ?? raw.latencyMs),
    ok: raw.ok !== false && !raw.error,
    error: String(raw.error ?? ''),
    ts: toMs(raw.ts ?? raw.timestamp ?? raw.created_at),
  };
}

function normalizeTraceItems(raw) {
  const items = Array.isArray(raw) ? raw : raw?.items;
  return (Array.isArray(items) ? items : []).map(normalizeTraceItem).filter(Boolean);
}

function normalizeTraceDetail(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const events = Array.isArray(raw.events)
    ? raw.events
    : Array.isArray(raw.steps)
      ? raw.steps
      : [];
  return {
    traceId: String(raw.trace_id ?? raw.traceId ?? ''),
    conversationId: String(raw.conversation_id ?? raw.conversationId ?? ''),
    latencyMs: num(raw.latency_ms ?? raw.latencyMs) ?? num(raw.total_latency_ms),
    tokensIn: num(raw.tokens_in ?? raw.tokensIn),
    tokensOut: num(raw.tokens_out ?? raw.tokensOut),
    toolCalls: num(raw.tool_calls ?? raw.tool_call_count) ?? events.filter((e) => e?.tool).length,
    ok: raw.ok !== false && !raw.error,
    error: String(raw.error ?? ''),
    events: events.map((event, index) => ({
      step: num(event?.step) ?? index + 1,
      event: String(event?.event ?? event?.name ?? ''),
      tool: String(event?.tool ?? event?.tool_name ?? ''),
      latencyMs: num(event?.latency_ms ?? event?.latencyMs),
      ok: event?.ok !== false && !event?.error,
      error: String(event?.error ?? ''),
      tokensIn: num(event?.tokens_in),
      tokensOut: num(event?.tokens_out),
      ts: toMs(event?.ts ?? event?.timestamp),
    })),
  };
}

function normalizeRag(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const values = [
    ['recall@5', pickRagValue(raw, ['recall_at_5', 'recall@5', 'recall5', 'recall_at5'])],
    ['MRR', pickRagValue(raw, ['mrr', 'MRR'])],
    ['引用准确率', pickRagValue(raw, ['citation_accuracy', 'citation_acc', 'citationAccuracy'])],
    ['faithfulness', pickRagValue(raw, ['faithfulness', 'faithfulness_score'])],
  ];
  if (values.every(([, value]) => value === null || value === undefined)) return null;
  const samples = num(raw.samples ?? raw.sample_count ?? raw.count);
  return { values, samples };
}

export class ObservabilityPanel {
  constructor(root = document, handlers = {}) {
    this.root = root;
    this.handlers = handlers;
    this._loading = false;
    this._loaded = false;
    this._lastLoadAt = 0;
    this._currentConversationId = null;
    this._expanded = new Map();
    this._debug = {
      loaded: false,
      metricsAvailable: false,
      tracesCount: 0,
      ragAvailable: false,
      error: '',
      lastUpdatedAt: null,
    };
    const byId = (id) => root.getElementById(id);
    this.el = {
      status: byId('obs-status'),
      refresh: byId('obs-refresh'),
      filter: byId('obs-filter-current'),
      metrics: {
        latency: byId('obs-metric-latency'),
        errorRate: byId('obs-metric-error-rate'),
        tokens: byId('obs-metric-tokens'),
      },
      traceList: byId('obs-trace-list'),
      rag: byId('obs-rag'),
      ragGrid: byId('obs-rag-grid'),
    };
    this._bindEvents();
  }

  _bindEvents() {
    this.el.refresh?.addEventListener('click', () => this.load({ force: true }));
    this.el.filter?.addEventListener('change', () => this.load({ force: true }));
    this.el.traceList?.addEventListener('click', (event) => {
      const head = event.target.closest('.obs-trace-head');
      if (!head) return;
      const row = head.closest('.obs-trace');
      if (row) this.toggleTrace(row);
    });
  }

  setCurrentConversation(conversationId) {
    this._currentConversationId = conversationId ? String(conversationId) : null;
    if (this.el.filter && !this._currentConversationId) this.el.filter.checked = false;
  }

  _setStatus(text, tone = '') {
    const node = this.el.status;
    if (!node) return;
    node.textContent = text;
    node.classList.remove('ok', 'warn', 'error');
    if (tone) node.classList.add(tone);
  }

  _setMetric(card, value, sub) {
    if (!card) return;
    const valueNode = card.querySelector('.obs-metric-value');
    const subNode = card.querySelector('.obs-metric-sub');
    if (valueNode) valueNode.textContent = value;
    if (subNode) subNode.textContent = sub || '';
  }

  _renderMetrics(metrics) {
    const latencyCard = this.el.metrics.latency;
    const errorCard = this.el.metrics.errorRate;
    const tokenCard = this.el.metrics.tokens;
    for (const card of [latencyCard, errorCard, tokenCard]) {
      card?.classList.remove('unavailable');
      card?.classList.add('available');
    }
    this._setMetric(
      latencyCard,
      metrics?.avgLatencyMs !== null && metrics?.avgLatencyMs !== undefined
        ? formatLatency(metrics.avgLatencyMs)
        : '—',
      metrics?.calls ? `${metrics.calls} 次调用` : 'Agent 平均耗时',
    );
    this._setMetric(
      errorCard,
      metrics?.toolErrorRate !== null && metrics?.toolErrorRate !== undefined
        ? formatRate(metrics.toolErrorRate)
        : '—',
      metrics?.byModel?.length ? `${metrics.byModel.length} 个模型` : '工具调用失败占比',
    );
    this._setMetric(
      tokenCard,
      metrics?.tokensTotal !== null && metrics?.tokensTotal !== undefined
        ? formatTokens(metrics.tokensTotal)
        : '—',
      metrics?.byDay?.length ? `近 ${metrics.byDay.length} 天` : '输入+输出 token',
    );
  }

  _renderMetricsUnavailable(errorMessage) {
    for (const card of [this.el.metrics.latency, this.el.metrics.errorRate, this.el.metrics.tokens]) {
      card?.classList.remove('available');
      card?.classList.add('unavailable');
      this._setMetric(card, '—', errorMessage || '指标服务未就绪');
    }
  }

  _traceHeadHtml(item) {
    const time = item.ts ? formatClock(item.ts / 1000) : '—';
    const eventText = item.event || (item.step ? `第 ${item.step} 步` : 'trace');
    const toolText = item.tool
      ? `⚙ ${item.tool}`
      : [item.provider, item.model].filter(Boolean).join('/') || '—';
    return `<button type="button" class="obs-trace-head" aria-expanded="false">
      <span class="obs-trace-time">${escapeHtml(time)}</span>
      <span class="obs-trace-event">${escapeHtml(eventText)}${item.step ? ` · #${item.step}` : ''}</span>
      <span class="obs-trace-tool">${escapeHtml(toolText)}</span>
      <span class="obs-trace-latency">${escapeHtml(formatLatency(item.latencyMs))}</span>
      <span class="obs-trace-status ${item.ok ? 'ok' : 'bad'}">${item.ok ? '✓' : '✕'}</span>
      <span class="obs-trace-caret">▸</span>
    </button>
    <div class="obs-trace-detail hidden"></div>`;
  }

  _renderTraces(items, { filterCurrent = false, noConversation = false } = {}) {
    const list = this.el.traceList;
    if (!list) return;
    if (noConversation) {
      list.innerHTML = '<li class="obs-empty">当前没有进行中的会话，无法按会话过滤</li>';
      return;
    }
    if (!items.length) {
      list.innerHTML = `<li class="obs-empty">${
        filterCurrent ? '当前会话暂无 trace 记录' : '暂无 trace 记录'
      }</li>`;
      return;
    }
    list.innerHTML = items
      .map(
        (item) => `<li class="obs-trace${item.ok ? '' : ' bad'}" data-trace-id="${escapeHtml(
          item.traceId,
        )}">
          ${this._traceHeadHtml(item)}
        </li>`,
      )
      .join('');
    this._expanded.clear();
  }

  async toggleTrace(row) {
    const detail = row.querySelector('.obs-trace-detail');
    const caret = row.querySelector('.obs-trace-caret');
    const traceId = row.dataset.traceId;
    if (!detail || !traceId) return;
    const willExpand = detail.classList.contains('hidden');
    if (!willExpand) {
      detail.classList.add('hidden');
      row.classList.remove('expanded');
      row.querySelector('.obs-trace-head')?.setAttribute('aria-expanded', 'false');
      if (caret) caret.textContent = '▸';
      this._expanded.delete(traceId);
      this._emitChange();
      return;
    }
    row.classList.add('expanded');
    row.querySelector('.obs-trace-head')?.setAttribute('aria-expanded', 'true');
    if (caret) caret.textContent = '▾';
    detail.classList.remove('hidden');
    this._expanded.set(traceId, true);
    if (detail.dataset.loaded === '1') {
      this._emitChange();
      return;
    }
    detail.innerHTML = '<div class="obs-trace-detail-loading">正在加载 trace 详情…</div>';
    try {
      const raw = await this.handlers.onLoadTrace?.(traceId);
      const trace = normalizeTraceDetail(raw) || { events: [] };
      const summary = [
        trace.latencyMs !== null && trace.latencyMs !== undefined
          ? `总耗时 ${formatLatency(trace.latencyMs)}`
          : '',
        trace.toolCalls ? `工具调用 ${trace.toolCalls} 次` : '',
        trace.tokensIn !== null && trace.tokensIn !== undefined
          ? `tokens 输入 ${formatTokens(trace.tokensIn)} / 输出 ${formatTokens(trace.tokensOut)}`
          : '',
        trace.error ? `错误：${trace.error}` : '',
      ]
        .filter(Boolean)
        .join(' · ');
      const eventsHtml = trace.events.length
        ? trace.events
            .map(
              (event) => `<li class="obs-trace-event-row${event.ok ? '' : ' bad'}">
                <span class="obs-trace-event-step">#${escapeHtml(String(event.step ?? '?'))}</span>
                <span class="obs-trace-event-name">${escapeHtml(event.event || 'event')}</span>
                <span class="obs-trace-event-tool">${event.tool ? escapeHtml(`⚙ ${event.tool}`) : '—'}</span>
                <span class="obs-trace-event-latency">${escapeHtml(formatLatency(event.latencyMs))}</span>
                <span class="obs-trace-event-ok">${event.ok ? '✓' : `✕ ${escapeHtml(event.error || '')}`}</span>
              </li>`,
            )
            .join('')
        : '<li class="obs-empty">该 trace 无事件明细</li>';
      detail.innerHTML = `
        <div class="obs-trace-summary">${escapeHtml(summary || '无汇总信息')}</div>
        <ol class="obs-trace-events">${eventsHtml}</ol>`;
      detail.dataset.loaded = '1';
    } catch (error) {
      detail.innerHTML = `<div class="obs-trace-detail-error">trace 详情加载失败：${escapeHtml(
        error?.message || String(error),
      )}</div>`;
    }
    this._emitChange();
  }

  _renderRag(raw) {
    const rag = normalizeRag(raw);
    if (!rag || !this.el.rag) return false;
    if (this.el.ragGrid) {
      this.el.ragGrid.innerHTML = rag.values
        .map(
          ([label, value]) => `<div class="obs-rag-item">
            <span class="obs-rag-label">${escapeHtml(label)}</span>
            <span class="obs-rag-value">${
              value === null || value === undefined
                ? '—'
                : label === 'MRR'
                  ? escapeHtml(num(value).toFixed(2))
                  : escapeHtml(formatRate(value))
            }</span>
          </div>`,
        )
        .join('');
    }
    this.el.rag.classList.remove('hidden');
    if (this.el.rag) {
      this.el.rag.title = rag.samples ? `评测样本：${rag.samples} 条` : 'RAG 评测指标';
    }
    return true;
  }

  async load({ force = false } = {}) {
    if (this._loading) return;
    if (!force && this._loaded && Date.now() - this._lastLoadAt < 15000) return;
    this._loading = true;
    this._setStatus('正在加载可观测性数据…');
    const filterCurrent = Boolean(this.el.filter?.checked);
    try {
      const result =
        (await this.handlers.onLoad?.({
          filterCurrent,
          conversationId: filterCurrent ? this._currentConversationId : null,
        })) || {};
      const metricsError = result.metricsError;
      const tracesError = result.tracesError;
      const ragError = result.ragError;
      const metrics = normalizeMetrics(result.metrics);
      const traces = normalizeTraceItems(result.traces);
      if (metrics) this._renderMetrics(metrics);
      else this._renderMetricsUnavailable(this._errorText(metricsError));
      this._renderTraces(traces, {
        filterCurrent,
        noConversation: Boolean(result.noConversation),
      });
      const ragAvailable = ragError ? false : this._renderRag(result.rag);
      if (!ragAvailable && this.el.rag) this.el.rag.classList.add('hidden');
      const unavailable = Boolean(metricsError) && Boolean(tracesError);
      if (unavailable) {
        this._setStatus(
          `可观测性服务未就绪（${this._errorText(metricsError)}），不影响其它功能`,
          'warn',
        );
      } else if (metricsError || tracesError) {
        this._setStatus('部分可观测性数据不可用（其余功能正常）', 'warn');
      } else {
        this._setStatus(
          `已更新 ${formatClock(Date.now() / 1000)} · ${traces.length} 条 trace`,
          'ok',
        );
      }
      this._debug = {
        loaded: true,
        metricsAvailable: Boolean(metrics),
        tracesCount: traces.length,
        ragAvailable: Boolean(ragAvailable),
        error: unavailable
          ? this._errorText(metricsError)
          : metricsError || tracesError
            ? 'partial'
            : '',
        lastUpdatedAt: new Date().toISOString(),
      };
      this._loaded = true;
      this._lastLoadAt = Date.now();
    } catch (error) {
      this._setStatus(`可观测性数据加载失败：${error?.message || error}`, 'error');
      this._debug = {
        loaded: true,
        metricsAvailable: false,
        tracesCount: 0,
        ragAvailable: false,
        error: error?.message || String(error),
        lastUpdatedAt: new Date().toISOString(),
      };
    } finally {
      this._loading = false;
      this.handlers.onChange?.(this._debug);
    }
  }

  _errorText(error) {
    if (!error) return '未知错误';
    if (error.kind === 'unsupported' || error.status === 404) return 'Agent W15 接口未上线';
    return error.message || String(error);
  }

  _emitChange() {
    this.handlers.onChange?.(this._debug);
  }

  reset() {
    this._loaded = false;
    this._expanded.clear();
    this._debug = {
      loaded: false,
      metricsAvailable: false,
      tracesCount: 0,
      ragAvailable: false,
      error: '',
      lastUpdatedAt: null,
    };
    this._renderMetricsUnavailable('等待连接 Agent…');
    if (this.el.traceList) this.el.traceList.innerHTML = '<li class="obs-empty">尚未加载</li>';
    if (this.el.rag) this.el.rag.classList.add('hidden');
    this._setStatus('打开页签后加载 Agent 可观测性数据');
    this.handlers.onChange?.(this._debug);
  }

  getDebug() {
    return { ...this._debug, loading: this._loading, expanded: this._expanded.size };
  }
}
