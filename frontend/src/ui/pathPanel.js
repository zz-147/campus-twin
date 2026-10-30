/**
 * W10 路径追踪面板（右侧信息坞第 4 个页签）。
 *
 * 交互：选择源/目的（来自 /api/hosts + /api/devices，含 pending 新设备）→「追踪」
 * → 调 POST /api/path-trace → 3D 场景高亮路径（CampusScene.setPathTrace）
 * → 面板列出 hop（编号/设备/角色/端口），partial/failed 显示断点与中文错误。
 *
 * 本模块只负责 DOM 与选择逻辑，3D 渲染由 CampusScene 完成，接口调用由 main.js 编排。
 */
import { escapeHtml, formatDuration } from './format.js';

const ROLE_LABELS = {
  source: '源',
  destination: '目的',
  transit: '途经',
  intermediate: '途经',
  unknown: '节点',
};

const STATUS_META = {
  complete: { label: '完整路径', tone: 'ok' },
  partial: { label: '部分可达 · 有断点', tone: 'warn' },
  failed: { label: '追踪失败', tone: 'error' },
};

/** 从场景条目里提取可用于路径追踪的端点（有 IP 的设备/主机/待确认设备） */
function endpointFromItem(item) {
  if (!item) return null;
  if (item.kind === 'host') {
    const ip = item.host?.ip;
    if (!ip) return null;
    return { ip, name: item.hostname, kind: '主机', detail: item.buildingName || '' };
  }
  if (item.kind === 'device') {
    const ip =
      item.device?.managementIp ||
      item.configDevice?.mgmt_ip ||
      item.configDevice?.ip ||
      item.configDevice?.management_ip;
    if (!ip) return null;
    return {
      ip,
      name: item.hostname,
      kind: item.pending ? '新设备' : item.typeLabel || '设备',
      detail: item.buildingName || '',
    };
  }
  return null;
}

export class PathPanel {
  constructor(root = document, handlers = {}) {
    this.root = root;
    this.handlers = handlers;
    this._endpoints = [];
    this._trace = null;
    this._busy = false;
    const byId = (id) => root.getElementById(id);
    this.el = {
      source: byId('path-source'),
      dest: byId('path-dest'),
      swap: byId('path-swap'),
      trace: byId('path-trace-btn'),
      clear: byId('path-clear-btn'),
      status: byId('path-status'),
      summary: byId('path-summary'),
      hops: byId('path-hops'),
    };
    this._bindEvents();
    this.clearView();
  }

  _bindEvents() {
    this.el.trace?.addEventListener('click', () => {
      if (this._busy) return;
      const sourceIp = this.el.source?.value || '';
      const destIp = this.el.dest?.value || '';
      if (!sourceIp || !destIp) {
        this.renderError('请选择源与目的地址');
        return;
      }
      if (sourceIp === destIp) {
        this.renderError('源与目的不能是同一地址');
        return;
      }
      this.handlers.onTrace?.(sourceIp, destIp);
    });
    this.el.clear?.addEventListener('click', () => this.handlers.onClear?.());
    this.el.swap?.addEventListener('click', () => {
      if (!this.el.source || !this.el.dest) return;
      const sourceValue = this.el.source.value;
      this.el.source.value = this.el.dest.value;
      this.el.dest.value = sourceValue;
    });
  }

  /** 场景数据刷新后更新端点下拉（保留当前选择；同一 IP 只出现一次） */
  setEndpoints(items = [], pendingDevices = []) {
    const seen = new Map();
    for (const item of items) {
      const endpoint = endpointFromItem(item);
      if (endpoint && !seen.has(endpoint.ip)) seen.set(endpoint.ip, endpoint);
    }
    for (const pending of pendingDevices || []) {
      const ip = pending.ip;
      if (!ip || seen.has(ip)) continue;
      seen.set(ip, {
        ip,
        name: pending.name,
        kind: '新设备',
        detail: pending.inferredBuilding || '',
      });
    }
    const endpoints = [...seen.values()].sort((a, b) => {
      const hostRank = (item) => (item.kind === '主机' ? 0 : item.kind === '新设备' ? 1 : 2);
      return hostRank(a) - hostRank(b) || a.name.localeCompare(b.name, 'zh-Hans-CN');
    });
    this._endpoints = endpoints;
    const previousSource = this.el.source?.value;
    const previousDest = this.el.dest?.value;
    const options = endpoints
      .map(
        (endpoint) =>
          `<option value="${escapeHtml(endpoint.ip)}">${escapeHtml(endpoint.name)} · ${escapeHtml(
            endpoint.ip,
          )}${endpoint.kind ? `（${escapeHtml(endpoint.kind)}）` : ''}</option>`,
      )
      .join('');
    if (this.el.source) {
      this.el.source.innerHTML = options;
      this.el.source.disabled = endpoints.length === 0;
    }
    if (this.el.dest) {
      this.el.dest.innerHTML = options;
      this.el.dest.disabled = endpoints.length === 0;
    }
    const restore = (select, value) => {
      if (!select) return;
      if (value && endpoints.some((endpoint) => endpoint.ip === value)) select.value = value;
    };
    restore(this.el.source, previousSource);
    restore(this.el.dest, previousDest);
    if (this.el.source && this.el.dest) {
      if (!this.el.source.value) this.el.source.value = endpoints[0]?.ip || '';
      if (!this.el.dest.value) {
        const other = endpoints.find((endpoint) => endpoint.ip !== this.el.source.value);
        this.el.dest.value = other?.ip || endpoints[0]?.ip || '';
      }
    }
    this._updateButtons();
  }

  _updateButtons() {
    if (this.el.trace) {
      this.el.trace.disabled = this._busy || !this._endpoints.length;
      this.el.trace.textContent = this._busy ? '追踪中…' : '追踪路径';
    }
    if (this.el.clear) this.el.clear.disabled = this._busy || !this._trace;
  }

  _setStatus(html, tone = '') {
    if (!this.el.status) return;
    this.el.status.innerHTML = html;
    this.el.status.classList.remove('ok', 'warn', 'error');
    if (tone) this.el.status.classList.add(tone);
  }

  setLoading(loading, text = '正在通过 NC flow-analysis 追踪路径…') {
    this._busy = Boolean(loading);
    this._updateButtons();
    if (loading) {
      this._setStatus(`<span class="path-spinner" aria-hidden="true"></span>${escapeHtml(text)}`);
    }
  }

  renderError(message) {
    this._busy = false;
    this._updateButtons();
    this._setStatus(`<span class="path-error-icon">✕</span>${escapeHtml(message || '路径追踪失败')}`, 'error');
  }

  /** 渲染一次路径追踪结果（3D 已由 CampusScene 完成） */
  renderTrace(trace) {
    this._busy = false;
    this._trace = trace || null;
    this._updateButtons();
    if (!trace) {
      this.clearView();
      return;
    }
    const status = String(trace.trace_status || trace.status || 'complete').toLowerCase();
    const statusMeta = STATUS_META[status] || { label: status, tone: 'warn' };
    const hops = Array.isArray(trace.hops) ? trace.hops : [];
    const elapsed = Number(trace.elapsed_ms) / 1000;
    const breakpoint = status !== 'complete' ? hops[hops.length - 1] : null;
    const source = hops[0]?.device_name || trace.source_ip || '—';
    const lastHopName = hops[hops.length - 1]?.device_name || trace.dest_ip || '—';
    const destination =
      status === 'complete'
        ? lastHopName
        : `${trace.dest_ip || lastHopName}（不可达）`;
    if (this.el.summary) {
      this.el.summary.innerHTML = `
        <div class="path-summary-line">
          <span class="path-endpoint source">${escapeHtml(source)}</span>
          <span class="path-arrow-line">→</span>
          <span class="path-endpoint dest">${escapeHtml(destination)}</span>
        </div>
        <div class="path-summary-meta">
          <span class="path-status-chip ${statusMeta.tone}">${escapeHtml(statusMeta.label)}</span>
          <span>${hops.length} 跳${elapsed ? ` · ${formatDuration(elapsed)}` : ''}${trace.cached ? ' · 缓存' : ''}</span>
        </div>`;
    }
    let statusHtml = `<span class="path-ok-icon">●</span>${escapeHtml(
      `${trace.source_ip || ''} → ${trace.dest_ip || ''} · ${statusMeta.label}`,
    )}`;
    let tone = statusMeta.tone;
    if (breakpoint) {
      statusHtml = `<span class="path-error-icon">✕</span>断点：${escapeHtml(
        breakpoint.device_name || '未知设备',
      )}${trace.error ? ` · ${escapeHtml(String(trace.error))}` : ''}`;
      tone = 'error';
    } else if (trace.error) {
      statusHtml = `<span class="path-error-icon">✕</span>${escapeHtml(String(trace.error))}`;
      tone = 'error';
    }
    this._setStatus(statusHtml, tone);

    if (!this.el.hops) return;
    if (!hops.length) {
      this.el.hops.innerHTML = '<li class="empty">未返回 hop 信息</li>';
      return;
    }
    this.el.hops.innerHTML = hops
      .map((hop, index) => {
        const role = String(hop.role || '').toLowerCase();
        const roleLabel = ROLE_LABELS[role] || ROLE_LABELS.unknown;
        const isBreak = breakpoint && index === hops.length - 1;
        const ports = [
          hop.ingress_port ? `入 ${hop.ingress_port}` : '',
          hop.egress_port ? `出 ${hop.egress_port}` : '',
        ]
          .filter(Boolean)
          .join(' · ');
        return `<li class="path-hop${isBreak ? ' breakpoint' : ''} role-${escapeHtml(role || 'unknown')}">
          <span class="path-hop-index">${index + 1}</span>
          <div class="path-hop-main">
            <div class="path-hop-head">
              <span class="path-hop-name">${escapeHtml(hop.device_name || `hop${index}`)}</span>
              <span class="path-hop-role ${escapeHtml(role || 'unknown')}">${escapeHtml(roleLabel)}</span>
              ${isBreak ? '<span class="path-hop-break">✕ 断点</span>' : ''}
            </div>
            <div class="path-hop-meta">
              ${ports ? `<span>${escapeHtml(ports)}</span>` : '<span>—</span>'}
            </div>
          </div>
        </li>`;
      })
      .join('');
    if (this.el.status) {
      this.el.status.title = trace.error || '';
    }
  }

  clearView() {
    this._busy = false;
    this._trace = null;
    this._updateButtons();
    this._setStatus(
      '选择源与目的设备后点击「追踪路径」；路径会在 3D 场景中发光高亮并标注 hop 编号。',
    );
    if (this.el.summary) this.el.summary.innerHTML = '';
    if (this.el.hops) this.el.hops.innerHTML = '<li class="empty">尚未追踪路径</li>';
  }

  get trace() {
    return this._trace;
  }
}
