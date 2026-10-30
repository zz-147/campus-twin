/**
 * DOM 大屏 HUD：顶部状态栏、告警列表、右侧信息坞（设备详情 / AI 助手 / 图例）、
 * 离线提示、加载遮罩、Toast。
 *
 * 所有展示内容一律来自 main.js 传入的后端快照 / WebSocket 告警，
 * 本文件不包含任何设备数据。
 *
 * W7 增强：
 * - 告警时间优先展示后端 observed_at（宿主机时间），tooltip 附 NC 原始 source_ts；
 * - 告警生命周期（active / cleared）用状态标签区分；
 * - 状态栏「数据新鲜度」按秒刷新（来自 /api/hosts 的 observed_at + snapshot_age_seconds）。
 */
import { STATUS_META } from '../api/normalize.js';
import { alertStateLabel } from '../api/alertsView.js';
import { clamp, escapeHtml, formatClock, formatDateTime, formatPercent } from './format.js';

const LEGEND_ITEMS = [
  { kind: 'dot', color: STATUS_META.managed.css, label: '已纳管 / 在线（Managed）' },
  { kind: 'dot', color: STATUS_META.unreachable.css, label: '不可达（Unreachable）' },
  { kind: 'dot', color: STATUS_META.unsupported.css, label: '不支持（Unsupported）' },
  { kind: 'dot', color: STATUS_META.unknown.css, label: '无数据（蓝灰）' },
  { kind: 'ring', color: '#ff3b52', label: '告警设备 · 脉冲光环' },
  { kind: 'diamond', color: '#54e6ff', label: 'NC 数据源控制器' },
  { kind: 'box', color: '#2f6f9e', label: '楼栋体块（半透明）' },
  { kind: 'line', color: '#2f86c4', label: '设备间链路' },
];

// 状态文案统一由 api/alertsView.js 的 alertStateLabel() 生成（含 stale_source 语义）
function sourceLabel(source, demo) {
  if (demo) return '演示数据';
  switch (String(source || '').toLowerCase()) {
    case 'netcon':
      return 'PT NC 真实采集';
    case 'simulator':
      return '行为仿真';
    default:
      return source || '—';
  }
}

/** 秒数 → 易读延迟文本 */
export function formatAge(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '—';
  const value = Math.max(0, Number(seconds));
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)} 秒前`;
  return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒前`;
}

export class Hud {
  constructor(root = document, handlers = {}) {
    this.root = root;
    this.handlers = handlers;
    this._demoActive = false;
    this._connection = { online: false, wsOpen: false, apiBase: '', error: '' };
    this._lastSnapshot = null;
    this._activeTab = 'detail';
    this._toastTimer = null;

    const byId = (id) => root.getElementById(id);
    this.el = {
      health: byId('stat-health'),
      healthBar: byId('stat-health-bar'),
      devices: byId('stat-devices'),
      devicesSub: byId('stat-devices-sub'),
      hosts: byId('stat-hosts'),
      alerts: byId('stat-alerts'),
      alertsSub: byId('stat-alerts-sub'),
      collect: byId('stat-collect'),
      freshness: byId('stat-freshness'),
      freshnessSub: byId('stat-freshness-sub'),
      conn: byId('stat-conn'),
      api: byId('stat-api'),
      source: byId('stat-source'),

      alertsList: byId('alerts-list'),
      alertCount: byId('alert-count'),
      alertsFoot: byId('alerts-foot'),
      clearCleared: byId('btn-clear-cleared'),
      sceneMeta: byId('scene-meta'),
      // W10：视图切换 / 通知状态
      alertViewButtons: Array.from(root.querySelectorAll('.alert-view-btn')),
      alertGroupsHint: byId('alert-groups-hint'),
      notifyStat: byId('stat-notify'),
      notifyValue: byId('stat-notify-value'),
      notifySub: byId('stat-notify-sub'),
      notifyTest: byId('btn-notify-test'),

      detailBody: byId('detail-body'),
      detailClose: byId('btn-close-detail'),

      tabs: Array.from(root.querySelectorAll('.dock-tab')),
      panes: Array.from(root.querySelectorAll('.dock-pane')),
      aiTabDot: byId('ai-tab-dot'),

      legend: byId('legend'),
      tooltip: byId('tooltip'),
      toast: byId('toast'),
      runtimeError: byId('runtime-error'),

      offlineBanner: byId('offline-banner'),
      offlineDetail: byId('offline-detail'),
      demoButton: byId('btn-demo'),
      retryButton: byId('btn-retry'),

      demoBadge: byId('demo-badge'),

      loadingOverlay: byId('loading-overlay'),
      loadingText: byId('loading-text'),
      loadingSub: byId('loading-sub'),

      rotateToggle: byId('toggle-rotate'),
      mockToggle: byId('toggle-mock'),
      refreshButton: byId('btn-refresh'),
      resetButton: byId('btn-reset'),
    };

    this._bindEvents();
    this._renderLegend();
    // 「数据新鲜度」按秒推进（不依赖新快照到达）
    this._freshnessTimer = setInterval(() => this._updateFreshness(), 1000);
  }

  _bindEvents() {
    const { el, handlers } = this;
    el.rotateToggle?.addEventListener('change', () =>
      handlers.onToggleRotate?.(el.rotateToggle.checked),
    );
    el.mockToggle?.addEventListener('change', () =>
      handlers.onToggleMock?.(el.mockToggle.checked),
    );
    el.refreshButton?.addEventListener('click', () => handlers.onRefresh?.());
    el.resetButton?.addEventListener('click', () => handlers.onResetView?.());
    el.retryButton?.addEventListener('click', () => handlers.onRefresh?.());
    el.demoButton?.addEventListener('click', () => {
      if (el.mockToggle) el.mockToggle.checked = true;
      handlers.onToggleMock?.(true);
    });
    el.detailClose?.addEventListener('click', () => handlers.onDeselect?.());
    el.clearCleared?.addEventListener('click', () => handlers.onClearCleared?.());
    // W10：全部 / 按根因分组 视图切换
    for (const button of el.alertViewButtons) {
      button.addEventListener('click', () => {
        const view = button.dataset.alertView === 'groups' ? 'groups' : 'all';
        this.setAlertView(view);
        handlers.onAlertViewChange?.(view);
      });
    }
    // W10：发送测试通知
    el.notifyTest?.addEventListener('click', () => handlers.onNotifyTest?.());
    el.alertsList?.addEventListener('click', (event) => {
      // W10：点击分组卡片头部展开/收起成员（成员内按钮与告警行点击不受影响）
      const groupHead = event.target.closest('.alert-group-head');
      if (groupHead) {
        groupHead.closest('.alert-group')?.classList.toggle('expanded');
        return;
      }
      // W9：条目上的管理按钮（删除/静默）不应触发行点击定位
      const actionButton = event.target.closest('button[data-alert-action]');
      if (actionButton) {
        event.stopPropagation();
        handlers.onAlertAction?.({
          action: actionButton.dataset.alertAction,
          alertId: actionButton.dataset.alertId,
          entryKey: actionButton.dataset.entryKey,
          device: actionButton.dataset.device,
        });
        return;
      }
      const node = event.target.closest('.alert-item');
      if (!node) return;
      for (const active of el.alertsList.querySelectorAll('.alert-item.active')) {
        active.classList.remove('active');
      }
      node.classList.add('active');
      handlers.onAlertDevice?.(node.dataset.device || '');
    });
    // 右侧信息坞页签
    for (const tab of el.tabs) {
      tab.addEventListener('click', () => this.switchTab(tab.dataset.tab));
    }
  }

  _renderLegend() {
    if (!this.el.legend) return;
    this.el.legend.innerHTML = LEGEND_ITEMS.map(
      (item) =>
        `<li><span class="legend-glyph ${item.kind}" style="--glyph-color:${item.color}"></span>${escapeHtml(
          item.label,
        )}</li>`,
    ).join('');
  }

  // ------------------------------------------------------------------ //
  // 右侧信息坞页签
  // ------------------------------------------------------------------ //
  switchTab(tabName) {
    if (!tabName) return;
    this._activeTab = tabName;
    for (const tab of this.el.tabs) {
      const active = tab.dataset.tab === tabName;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    }
    for (const pane of this.el.panes) {
      pane.classList.toggle('active', pane.dataset.pane === tabName);
    }
  }

  /** AI 服务状态同步到页签小圆点（online=绿 / 离线=红） */
  setAiStatus({ online } = {}) {
    const dot = this.el.aiTabDot;
    if (!dot) return;
    dot.classList.toggle('ok', Boolean(online));
    dot.classList.toggle('bad', !online);
    dot.title = online ? 'AI 服务已连接' : 'AI 服务未连接';
  }

  // ------------------------------------------------------------------ //
  // 顶部状态栏
  // ------------------------------------------------------------------ //
  updateStats(snapshot, options = {}) {
    const demo = Boolean(options.demo);
    this._lastSnapshot = snapshot;
    const setText = (element, text) => {
      if (element) element.textContent = text;
    };
    const setClass = (element, className, enabled) => {
      if (element) element.classList.toggle(className, enabled);
    };

    if (!snapshot) {
      setText(this.el.health, '—');
      if (this.el.healthBar) this.el.healthBar.style.width = '0%';
      setText(this.el.devices, '—');
      setText(this.el.devicesSub, '后端未连接');
      setText(this.el.hosts, '—');
      setText(this.el.alerts, '—');
      setText(this.el.alertsSub, '后端未连接');
      setText(this.el.collect, '—');
      setText(this.el.source, '—');
      setClass(this.el.alerts, 'bad', false);
      setClass(this.el.alerts, 'ok', false);
      setClass(this.el.collect, 'warn', false);
      setClass(this.el.collect, 'bad', true);
      this._updateFreshness();
      return;
    }

    const summary = snapshot.summary || {};
    const healthPercent = summary.healthPercent;
    const deviceRate =
      summary.devicesTotal > 0 ? (summary.devicesOnline / summary.devicesTotal) * 100 : null;

    setText(this.el.health, formatPercent(healthPercent));
    if (this.el.healthBar) {
      this.el.healthBar.style.width = `${clamp(healthPercent ?? 0, 0, 100)}%`;
      setClass(this.el.healthBar, 'warn', healthPercent !== null && healthPercent < 80);
      setClass(this.el.healthBar, 'bad', healthPercent !== null && healthPercent < 50);
    }

    setText(this.el.devices, `${summary.devicesOnline ?? 0}/${summary.devicesTotal ?? 0}`);
    setText(this.el.devicesSub, `在线率 ${formatPercent(deviceRate)}`);
    setText(this.el.hosts, String(summary.hostsTotal ?? 0));

    // 活跃告警（W3 生命周期统计）
    const activeAlerts = summary.alertsActive ?? 0;
    setText(this.el.alerts, String(activeAlerts));
    setText(
      this.el.alertsSub,
      `已恢复 ${summary.alertsCleared ?? 0} · 共 ${summary.alertsTotal ?? 0}`,
    );
    setClass(this.el.alerts, 'bad', activeAlerts > 0);
    setClass(this.el.alerts, 'ok', activeAlerts === 0);

    const collectStale = Boolean(snapshot.stale);
    const driver = snapshot.collect?.driver;
    setText(
      this.el.collect,
      collectStale ? '陈旧 / NC 离线' : driver ? `${driver} 正常` : '正常',
    );
    setClass(this.el.collect, 'warn', collectStale);
    setClass(this.el.collect, 'bad', false);
    setClass(this.el.collect, 'ok', !collectStale);

    setText(this.el.source, sourceLabel(snapshot.source, demo));
    this._updateFreshness();
  }

  /** 状态栏「数据更新于 X 秒前」：用后端 observed_at + snapshot_age_seconds 推进 */
  _updateFreshness() {
    const element = this.el.freshness;
    if (!element) return;
    const snapshot = this._lastSnapshot;
    const setSub = (text) => {
      if (this.el.freshnessSub) this.el.freshnessSub.textContent = text;
    };
    if (!snapshot) {
      element.textContent = '—';
      element.title = '后端未连接';
      element.classList.remove('ok', 'warn', 'bad');
      setSub('后端未连接');
      return;
    }

    const freshness = snapshot.freshness || {};
    let ageSeconds = null;
    if (typeof freshness.snapshotAgeSeconds === 'number') {
      // 后端响应时刻已算好的快照延迟，收到后按本地时间继续推进
      ageSeconds =
        freshness.snapshotAgeSeconds + (Date.now() - (snapshot.receivedAt || Date.now())) / 1000;
    } else if (freshness.observedAt) {
      ageSeconds = Date.now() / 1000 - Number(freshness.observedAt);
    }

    if (snapshot.stale) {
      element.textContent = '数据陈旧';
      element.title = '后端采集陈旧（NC 离线或采集失败）';
      element.classList.remove('ok', 'warn');
      element.classList.add('bad');
    } else if (ageSeconds === null) {
      element.textContent = '—';
      element.title = '后端未提供观测时间';
      element.classList.remove('ok', 'warn', 'bad');
    } else {
      element.textContent = formatAge(ageSeconds);
      element.title = `数据更新于 ${formatAge(ageSeconds)}（后端 observed_at）`;
      element.classList.toggle('ok', ageSeconds <= 15);
      element.classList.toggle('warn', ageSeconds > 15 && ageSeconds <= 60);
      element.classList.toggle('bad', ageSeconds > 60);
    }
    const observed = freshness.observedAtIso || freshness.observedAt;
    setSub(observed ? `观测 ${formatClock(observed)}` : '—');
  }

  setConnection({ online, wsOpen = false, apiBase = '', error = '' } = {}) {
    this._connection = { online, wsOpen, apiBase, error };
    const { el } = this;
    if (el.conn) {
      el.conn.textContent = online ? '已连接' : '未连接';
      el.conn.classList.toggle('ok', Boolean(online));
      el.conn.classList.toggle('bad', !online);
      el.conn.title = error || (online ? 'REST + WebSocket 通道正常' : '无法访问后端 API');
    }
    if (el.api) {
      const shortBase = String(apiBase || '').replace(/^https?:\/\//, '');
      el.api.textContent = shortBase ? `${shortBase} · ${wsOpen ? 'WS' : 'HTTP'}` : '';
      el.api.title = error || '';
    }
    if (!el.offlineBanner) return;
    if (online) {
      el.offlineBanner.classList.add('hidden');
      return;
    }
    el.offlineBanner.classList.remove('hidden');
    if (el.offlineDetail) {
      el.offlineDetail.textContent = this._demoActive
        ? '当前展示内置演示数据；可点击「重试」连接真实后端'
        : `无法获取真实采集数据${error ? `：${error}` : ''}`;
    }
    el.demoButton?.classList.toggle('hidden', this._demoActive);
  }

  setDemo(active) {
    this._demoActive = Boolean(active);
    this.el.demoBadge?.classList.toggle('hidden', !this._demoActive);
    if (this.el.mockToggle) this.el.mockToggle.checked = this._demoActive || this.el.mockToggle.checked;
    if (!this._connection.online) {
      this.setConnection(this._connection);
    }
  }

  setLoading(visible, title, sub) {
    if (!this.el.loadingOverlay) return;
    this.el.loadingOverlay.classList.toggle('hidden', !visible);
    if (title) this.el.loadingText.textContent = title;
    if (sub) this.el.loadingSub.textContent = sub;
  }

  setError(message) {
    if (!this.el.runtimeError) return;
    if (!message) {
      this.el.runtimeError.classList.add('hidden');
      this.el.runtimeError.textContent = '';
      return;
    }
    this.el.runtimeError.textContent = message;
    this.el.runtimeError.classList.remove('hidden');
  }

  toast(message, type = 'info', options = {}) {
    const element = this.el.toast;
    if (!element) return;
    element.innerHTML = '';
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    element.appendChild(text);
    element.classList.remove('hidden', 'warn', 'error', 'has-action');
    if (type === 'warn' || type === 'error') element.classList.add(type);
    clearTimeout(this._toastTimer);
    const hasAction = Boolean(options.actionLabel && typeof options.onAction === 'function');
    if (hasAction) {
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'toast-action';
      action.textContent = options.actionLabel;
      action.addEventListener(
        'click',
        () => {
          element.classList.add('hidden');
          options.onAction();
        },
        { once: true },
      );
      element.appendChild(action);
      element.classList.add('has-action');
    }
    const duration = options.duration || (hasAction ? 10000 : 3200);
    this._toastTimer = setTimeout(() => element.classList.add('hidden'), duration);
  }

  /** 告警面板顶部「清空已恢复（N）」按钮状态 */
  setClearClearedCount(count = 0) {
    const button = this.el.clearCleared;
    if (!button) return;
    const value = Math.max(0, Number(count) || 0);
    button.textContent = `清空已恢复（${value}）`;
    button.disabled = value <= 0;
    button.title =
      value > 0 ? `将软删除 ${value} 条已恢复告警（可审计）` : '当前没有可清空的已恢复告警';
  }

  /** 右侧图例页签展示当前场景配置来源/版本 */
  setSceneMeta(meta = {}) {
    const element = this.el.sceneMeta;
    if (!element) return;
    const source = meta.source === 'api' ? '后端配置' : '默认布局';
    const counts = meta.counts || {};
    const pendingCount = meta.pending ?? counts.pending ?? 0;
    element.textContent =
      `场景 ${source} · v${meta.version ?? 0} · ${counts.buildings ?? 0} 栋楼 · ` +
      `${counts.devices ?? 0} 设备${pendingCount ? ` · ${pendingCount} 待确认` : ''}`;
    element.classList.toggle('fallback', meta.source !== 'api');
    element.title =
      meta.source === 'api'
        ? '楼栋/设备位置来自后端 GET /api/scene（配置驱动）'
        : '后端 /api/scene 不可用，已回退默认 4 栋楼布局';
  }

  // ------------------------------------------------------------------ //
  // 告警列表（W3 生命周期 + observed_at / source_ts）
  // ------------------------------------------------------------------ //
  renderAlerts(entries = [], meta = {}) {
    const { el } = this;
    if (!el.alertsList) return;
    // W7.1：活跃只统计真正的 active（stale_source 的 issue 不算活跃）
    const activeCount =
      Number(meta.active) ||
      entries.filter((entry) => entry.state === 'active' && !entry.staleSource).length;
    const clearedCount =
      Number(meta.cleared) ||
      entries.filter((entry) => entry.state === 'cleared' || entry.staleSource).length;
    if (el.alertCount) {
      el.alertCount.textContent = String(activeCount);
      el.alertCount.classList.toggle('ok', activeCount === 0);
      el.alertCount.title = `活跃告警 ${activeCount} 条 · 已恢复 ${clearedCount} 条`;
    }
    // W9：顶部「清空已恢复（N）」按钮（不依赖 Agent 的按钮操作）
    this.setClearClearedCount(clearedCount);

    const detection = meta.detection || this._lastSnapshot?.detection || null;
    const engineText = detection?.engine ? `检测引擎 ${detection.engine}` : '检测引擎 —';
    const rulesTitle = detection?.rules?.length
      ? `检测规则（${detection.rules.length} 条）：\n${detection.rules.join('\n')}`
      : '';
    const staleCount = entries.filter((entry) => entry.staleSource).length;
    if (el.alertsFoot) {
      el.alertsFoot.textContent =
        `活跃 ${activeCount} · 已恢复 ${clearedCount} · ${engineText}` +
        (staleCount ? ` · NC 滞后 ${staleCount}` : '');
      el.alertsFoot.title = rulesTitle;
    }

    if (!entries.length) {
      el.alertsList.innerHTML = '<li class="empty">暂无告警</li>';
      return;
    }

    el.alertsList.innerHTML = entries.map((entry) => this._alertRowHtml(entry)).join('');
  }

  /** 单条告警行 HTML（列表与根因分组视图共用；保留删除/静默按钮） */
  _alertRowHtml(entry, { suppressed = false, suppressedBy = '', groupId = '', inGroup = false } = {}) {
    const severity = ['critical', 'warning', 'info'].includes(entry.severity)
      ? entry.severity
      : 'warning';
    const stale = Boolean(entry.staleSource);
    const state = ['active', 'cleared', 'info', 'silenced'].includes(entry.state)
      ? entry.state
      : 'active';
    const stateLabel = alertStateLabel(entry);
    // W9：告警数据管理按钮（仅检测引擎告警有后端 id；NC issue 由后端生命周期管理）
    const manageable =
      entry.sourceType === 'alert' &&
      entry.alertId !== null &&
      entry.alertId !== undefined &&
      entry.alertId !== '';
    const actionButtons = [
      state === 'active' && manageable
        ? `<button type="button" class="alert-act-btn" data-alert-action="silence"
            data-alert-id="${escapeHtml(String(entry.alertId))}"
            data-entry-key="${escapeHtml(entry.key)}"
            data-device="${escapeHtml(entry.device || '')}"
            title="静默该活跃告警（不删除，停止提醒）">静默</button>`
        : '',
      state === 'cleared' && !stale && manageable
        ? `<button type="button" class="alert-act-btn danger" data-alert-action="delete"
            data-alert-id="${escapeHtml(String(entry.alertId))}"
            data-entry-key="${escapeHtml(entry.key)}"
            data-device="${escapeHtml(entry.device || '')}"
            title="删除该条已恢复告警（软删除，可撤销）">删除</button>`
        : '',
    ]
      .filter(Boolean)
      .join('');
    const observed = entry.time ?? entry.observedAt ?? entry.ts;
    const time = formatClock(observed);
    const timeTitle = [
      `观测时间（observed_at）：${formatDateTime(observed)}`,
      entry.sourceTs ? `NC 源时间（source_ts）：${entry.sourceTs}` : '',
      entry.resolvedAt ? `恢复时间：${formatDateTime(entry.resolvedAt)}` : '',
      entry.clearReason ? `恢复原因：${entry.clearReason}` : '',
      stale ? 'NC 问题记录滞后：数据源实际状态已恢复（stale_source）' : '',
      suppressed ? `被上游根因抑制（suppressed_by=${suppressedBy || groupId || '—'}）` : '',
    ]
      .filter(Boolean)
      .join('\n');
    const device = entry.device || '—';
    const description = entry.description || '';
    const title = entry.title && entry.title !== description ? `${entry.title} · ` : '';
    const itemTitle = stale
      ? 'NC 问题记录滞后：数据源实际状态已恢复（stale_source）· 点击定位到相关设备'
      : `点击定位到相关设备${entry.sourceTs ? ` · NC 源时间 ${entry.sourceTs}` : ''}` +
        `${entry.clearReason ? ` · 恢复原因：${entry.clearReason}` : ''}`;
    return `<li class="alert-item ${severity} state-${state}${stale ? ' stale-source' : ''}${
      inGroup || suppressed ? ' alert-member' : ''
    }${suppressed ? ' suppressed' : ''}" data-device="${escapeHtml(device)}" data-key="${escapeHtml(
      entry.key,
    )}" data-alert-id="${escapeHtml(String(entry.alertId ?? ''))}" data-group-id="${escapeHtml(
      groupId || '',
    )}" title="${escapeHtml(itemTitle)}">
      <span class="alert-dot"></span>
      <div class="alert-main">
        <div class="alert-top">
          <span class="alert-device">${escapeHtml(device)}</span>
          ${
            suppressed
              ? '<span class="alert-member-suppressed-tag">被上游抑制</span>'
              : inGroup
                ? '<span class="alert-member-root-tag">根因</span>'
                : ''
          }
          <span class="alert-state${stale ? ' stale' : ''}">${escapeHtml(stateLabel)}</span>
          <time title="${escapeHtml(timeTitle)}">${escapeHtml(time)}</time>
        </div>
        <div class="alert-desc">${escapeHtml(`${title}${description}`)}</div>
        ${
          suppressed && suppressedBy
            ? `<div class="alert-member-extra"><span class="suppressed-by">suppressed_by=${escapeHtml(
                String(suppressedBy),
              )}</span></div>`
            : ''
        }
        ${actionButtons ? `<div class="alert-actions">${actionButtons}</div>` : ''}
      </div>
    </li>`;
  }

  // ------------------------------------------------------------------ //
  // W10：告警根因分组视图
  // ------------------------------------------------------------------ //
  setAlertView(view = 'all') {
    const normalized = view === 'groups' ? 'groups' : 'all';
    for (const button of this.el.alertViewButtons || []) {
      button.classList.toggle('active', (button.dataset.alertView || 'all') === normalized);
    }
    this._alertView = normalized;
  }

  setGroupsHint(text = '', tone = '') {
    const hint = this.el.alertGroupsHint;
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle('hidden', !text);
    hint.classList.remove('warn', 'error');
    if (tone) hint.classList.add(tone);
  }

  /**
   * 渲染根因分组视图。
   * @param {Array} groups GET /api/alerts/groups 的 groups
   * @param {Array} entries buildAlertEntries() 结果（用于按 alert id 复用条目）
   * @param {object} meta active/cleared/detection
   */
  renderAlertGroups(groups = [], entries = [], meta = {}) {
    const el = this.el;
    if (!el.alertsList) return;
    const activeCount =
      Number(meta.active) ||
      entries.filter((entry) => entry.state === 'active' && !entry.staleSource).length;
    const clearedCount =
      Number(meta.cleared) ||
      entries.filter((entry) => entry.state === 'cleared' || entry.staleSource).length;
    if (el.alertCount) {
      el.alertCount.textContent = String(activeCount);
      el.alertCount.classList.toggle('ok', activeCount === 0);
      el.alertCount.title = `活跃告警 ${activeCount} 条 · 已恢复 ${clearedCount} 条`;
    }
    this.setClearClearedCount(clearedCount);

    if (!Array.isArray(groups) || !groups.length) {
      el.alertsList.innerHTML =
        '<li class="alert-groups-empty">当前没有活跃告警，无法按根因分组</li>';
      if (el.alertsFoot) {
        el.alertsFoot.textContent = `活跃 ${activeCount} · 已恢复 ${clearedCount} · 根因分组 0 组`;
      }
      return;
    }

    const entryByAlertId = new Map();
    for (const entry of entries) {
      if (entry.alertId !== null && entry.alertId !== undefined) {
        entryByAlertId.set(String(entry.alertId), entry);
      }
    }
    const rootCauseLabels = {
      link_down: '链路中断',
      device_unreachable: '设备不可达',
      device_offline: '设备离线',
      host_down: '主机离线',
      device_down: '设备宕机',
      interface_down: '接口中断',
    };
    let suppressedTotal = 0;
    const groupsHtml = groups
      .map((group, groupIndex) => {
        const severity = ['critical', 'warning', 'info'].includes(group.severity)
          ? group.severity
          : 'warning';
        const members = Array.isArray(group.members) ? group.members : [];
        const suppressed =
          Number(group.suppressed_count) ||
          members.filter((member) => member.suppressed_by).length;
        suppressedTotal += suppressed;
        const rootCause = String(group.root_cause || 'unknown');
        const rootLabel = rootCauseLabels[rootCause] || rootCause;
        const firstSeen = Number(group.first_seen)
          ? formatClock(group.first_seen)
          : '—';
        const membersHtml = members
          .map((member) => {
            const memberEntry =
              entryByAlertId.get(String(member.id)) || {
                key: `alert-group:${member.id}`,
                device: member.device_name || group.root_device_name || '—',
                title: member.alert_type || '告警',
                description: member.detail || '',
                severity: member.severity || severity,
                state: 'active',
                sourceType: 'alert',
                alertId: member.id,
                staleSource: false,
                observedAt: member.last_seen || member.observed_at || group.last_seen,
              };
            return this._alertRowHtml(memberEntry, {
              suppressed: Boolean(member.suppressed_by),
              suppressedBy: member.suppressed_by || '',
              groupId: group.group_id || '',
              inGroup: true,
            });
          })
          .join('');
        const expanded = groupIndex === 0 ? ' expanded' : '';
        return `<li class="alert-group ${severity}${expanded}" data-group-id="${escapeHtml(
          String(group.group_id || ''),
        )}">
          <div class="alert-group-head" role="button" tabindex="0" title="点击展开/收起成员">
            <span class="alert-group-dot"></span>
            <div class="alert-group-main">
              <div class="alert-group-title">
                <span>${escapeHtml(group.title || rootLabel)}</span>
                <span class="alert-group-root">${escapeHtml(rootLabel)}</span>
              </div>
              <div class="alert-group-meta">
                <span>根因设备 ${escapeHtml(group.root_device_name || '—')}</span>
                <span>成员 ${Number(group.member_count) || members.length}</span>
                ${suppressed ? `<span class="suppressed-count">被抑制 ${suppressed}</span>` : ''}
                <span>首次 ${escapeHtml(firstSeen)}</span>
              </div>
            </div>
            <span class="alert-group-caret">▸</span>
          </div>
          <ul class="alert-group-members">${membersHtml || '<li class="empty">无成员</li>'}</ul>
        </li>`;
      })
      .join('');
    el.alertsList.innerHTML = groupsHtml;

    if (el.alertsFoot) {
      el.alertsFoot.textContent =
        `活跃 ${activeCount} · 已恢复 ${clearedCount} · 根因分组 ${groups.length} 组` +
        (suppressedTotal ? ` · 被抑制 ${suppressedTotal} 条` : '');
      el.alertsFoot.title = '告警按根因聚合：被上游抑制的成员仍保留、灰显展示，不删除、不改变 active 状态';
    }
  }

  // ------------------------------------------------------------------ //
  // W10：Webhook 通知状态
  // ------------------------------------------------------------------ //
  setNotifyStatus(status) {
    const stat = this.el.notifyStat;
    if (!stat) return;
    const typeLabels = {
      generic: '通用 Webhook',
      webhook: 'Webhook',
      dingtalk: '钉钉',
      feishu: '飞书',
      wecom: '企业微信',
    };
    if (!status || !status.available) {
      this.el.notifyValue && (this.el.notifyValue.textContent = '未配置');
      this.el.notifySub && (this.el.notifySub.textContent = '通知服务未就绪');
      stat.classList.remove('enabled');
      stat.classList.add('disabled');
      this.el.notifyTest?.classList.add('hidden');
      return;
    }
    if (status.enabled) {
      const typeLabel = typeLabels[status.type] || status.type || 'Webhook';
      this.el.notifyValue && (this.el.notifyValue.textContent = '已启用');
      this.el.notifySub &&
        (this.el.notifySub.textContent =
          `${typeLabel} · 已发送 ${status.sentCount || 0} 次` +
          (status.lastError ? ' · 最近失败' : ''));
      stat.classList.add('enabled');
      stat.classList.remove('disabled');
      this.el.notifyTest?.classList.remove('hidden');
      this.el.notifyTest?.removeAttribute('disabled');
      if (this.el.notifyTest) this.el.notifyTest.textContent = '测试';
      if (this.el.notifySub) {
        this.el.notifySub.title = status.lastError
          ? `最近错误：${status.lastError}`
          : status.lastSentAt
            ? `最近发送：${formatDateTime(status.lastSentAt)}`
            : '尚未发送过通知';
      }
    } else {
      this.el.notifyValue && (this.el.notifyValue.textContent = '未启用');
      this.el.notifySub &&
        (this.el.notifySub.textContent = 'NOTIFY_ENABLED=false');
      stat.classList.add('disabled');
      stat.classList.remove('enabled');
      this.el.notifyTest?.classList.add('hidden');
    }
  }

  setNotifyTestBusy(busy) {
    const button = this.el.notifyTest;
    if (!button) return;
    button.disabled = Boolean(busy);
    button.textContent = busy ? '发送中' : '测试';
  }

  // ------------------------------------------------------------------ //
  // 设备详情
  // ------------------------------------------------------------------ //
  setSelectedItem(item, { focusTab = false } = {}) {
    const { el } = this;
    if (!el.detailBody) return;
    el.detailClose?.classList.toggle('hidden', !item);

    if (!item) {
      el.detailBody.innerHTML = '<p class="empty">点击场景中的设备标记查看详情</p>';
      return;
    }

    // 用户点击设备时自动切到「设备详情」页签；后台数据刷新不抢页签
    if (focusTab) this.switchTab('detail');

    const status = item.statusMeta || STATUS_META.unknown;
    const rows = [];
    const push = (label, value) => {
      if (value === null || value === undefined) return;
      const text = String(value).trim();
      if (!text) return;
      rows.push([label, text]);
    };

    push('所属楼栋', `${item.buildingName}${item.fallbackBuilding ? '（自动归入）' : ''}`);

    if (item.kind === 'device') {
      const device = item.device || {};
      push('设备类型', item.typeLabel);
      push('管理 IP', device.managementIp);
      push('平台 / 型号', [device.platformId, device.productId].filter(Boolean).join(' / '));
      push('软件版本', device.softwareVersion);
      push('采集状态', device.collectionStatus);
      push('可达性', device.reachability);
      push('接口数', device.interfaceCount);
      push('运行时长', device.upTime);
      push('设备 ID', device.id);
    } else if (item.kind === 'host') {
      const host = item.host || {};
      push('主机类型', item.typeLabel);
      push('IPv4', host.ip);
      push('MAC 地址', host.mac);
      push('接入设备', host.connectedDeviceName);
      push('接入端口', host.connectedInterface);
      push('VLAN', host.vlanId);
      push('Ping 状态', host.pingStatus);
      push('主机 ID', host.id);
    } else {
      const controller = item.controller || {};
      push('角色', '数据源控制器（Packet Tracer Network Controller）');
      push('采集驱动', controller.driver);
      push('采集入口', controller.baseUrl);
      push('数据集来源', controller.datasetSource);
    }

    // W3 数据新鲜度：这条记录是宿主机什么时候观测到的
    if (item.observedAt) push('数据观测时间', formatClock(item.observedAt));
    if (typeof item.snapshotAgeSeconds === 'number') {
      push('快照延迟', `${item.snapshotAgeSeconds.toFixed(1)} 秒`);
    }

    push('当前状态', `${status.label}（${status.en}）`);
    const alertItems = item.alertItems || item.alertIssues || [];
    const activeAlertItems = alertItems.filter(
      (issue) => (issue.state || 'active') === 'active' && !issue.staleSource,
    );
    const staleAlertItems = alertItems.filter(
      (issue) => issue.staleSource || issue.state === 'cleared',
    );
    if (activeAlertItems.length) push('关联告警', `${activeAlertItems.length} 条`);
    if (staleAlertItems.length) {
      push('NC 滞后记录', `${staleAlertItems.length} 条（数据源已恢复）`);
    }

    const issuesHtml = alertItems.length
      ? `<div class="detail-issues">
          <h3>关联告警与问题</h3>
          <ul>${alertItems
            .map((issue) => {
              const state = issue.state || 'active';
              const stale = Boolean(issue.staleSource);
              const stateLabel = alertStateLabel(issue);
              const timeTitle = [
                issue.sourceTs ? `NC 源时间：${issue.sourceTs}` : '',
                stale ? 'NC 问题记录滞后：数据源实际状态已恢复（stale_source）' : '',
              ]
                .filter(Boolean)
                .join('\n');
              return `<li class="sev-${escapeHtml(issue.severity || 'warning')} state-${escapeHtml(
                state,
              )}${stale ? ' stale-source' : ''}">
                <span class="issue-name">${escapeHtml(issue.name || '健康问题')}<em>${escapeHtml(
                  stateLabel,
                )}</em></span>
                <span class="issue-desc">${escapeHtml(issue.description || '')}</span>
                <time title="${escapeHtml(
                  timeTitle,
                )}">${escapeHtml(formatClock(issue.time ?? issue.ts))}</time>
              </li>`;
            })
            .join('')}</ul>
        </div>`
      : '';

    const statusSource =
      item.kind === 'device'
        ? '后端 /api/devices（真实采集）'
        : item.kind === 'host'
          ? '后端 /api/hosts（真实采集）'
          : '后端 /api/health · driver_status（数据源状态）';

    el.detailBody.innerHTML = `
      <div class="detail-title">
        <span class="detail-name">${escapeHtml(item.hostname)}</span>
        <span class="status-badge" style="--badge-color:${status.css}">${escapeHtml(
          status.label,
        )} · ${escapeHtml(status.en)}</span>
      </div>
      <div class="detail-meta">${escapeHtml(item.typeLabel)} · ${
        item.alert ? '<span class="detail-alert">告警中</span>' : '正常'
      }</div>
      <dl class="detail-rows">
        ${rows
          .map(
            ([label, value]) =>
              `<div class="detail-row"><dt>${escapeHtml(label)}</dt><dd title="${escapeHtml(
                value,
              )}">${escapeHtml(value)}</dd></div>`,
          )
          .join('')}
      </dl>
      ${issuesHtml}
      <div class="detail-source">数据来源：${escapeHtml(statusSource)}</div>
    `;
  }

  // ------------------------------------------------------------------ //
  // 悬浮提示
  // ------------------------------------------------------------------ //
  showTooltip(item, event) {
    const tooltip = this.el.tooltip;
    if (!tooltip) return;
    if (!item || !event) {
      this.hideTooltip();
      return;
    }
    const status = item.statusMeta || STATUS_META.unknown;
    tooltip.innerHTML = `
      <strong>${escapeHtml(item.hostname)}</strong>
      <span>${escapeHtml(item.typeLabel)} · <i style="color:${status.css}">${escapeHtml(
        status.label,
      )}（${escapeHtml(status.en)}）</i></span>
      <span class="tip-sub">${escapeHtml(item.buildingName)}${
        item.alert ? ' · 告警中' : ''
      }</span>`;
    tooltip.classList.remove('hidden');
    const offset = 16;
    let left = event.clientX + offset;
    let top = event.clientY + offset;
    if (left + 240 > window.innerWidth) left = event.clientX - 248;
    if (top + 100 > window.innerHeight) top = event.clientY - 110;
    tooltip.style.left = `${Math.max(8, left)}px`;
    tooltip.style.top = `${Math.max(8, top)}px`;
  }

  hideTooltip() {
    this.el.tooltip?.classList.add('hidden');
  }

  // ------------------------------------------------------------------ //
  // 开关状态读取（供 main 初始化同步）
  // ------------------------------------------------------------------ //
  isAutoRotateEnabled() {
    return Boolean(this.el.rotateToggle?.checked);
  }

  isMockEnabled() {
    return Boolean(this.el.mockToggle?.checked);
  }

  dispose() {
    clearInterval(this._freshnessTimer);
    clearTimeout(this._toastTimer);
  }
}
