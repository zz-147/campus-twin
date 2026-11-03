/**
 * W16：通知设置对话框（飞书 / 钉钉 / 企业微信 Webhook 配置）。
 *
 * 入口：顶部「告警通知」stat 的齿轮按钮 #btn-notify-config。
 * 接口（task-33，未就绪时优雅降级）：
 * - GET  /api/notify/status → 回填状态区/表单
 * - POST /api/notify/config {enabled?,webhook_url?,webhook_type?,min_severity?,on_recovery?}
 * - POST /api/notify/test   → 发送测试通知
 *
 * 安全：Webhook URL 使用 password 输入框，仅通过请求体提交；页面不回显明文、
 * 不写 console/localStorage；后端返回明文时也会在展示层本地脱敏。
 */
import { escapeHtml, formatDateTime } from './format.js';

const TYPE_OPTIONS = [
  ['feishu', '飞书'],
  ['dingtalk', '钉钉'],
  ['wecom', '企业微信'],
  ['generic', '通用 Webhook'],
];

const PAYLOAD_KIND_LABELS = {
  new_alert: '新告警',
  alert: '告警',
  recovered: '恢复通知',
  recovery: '恢复通知',
  test: '测试消息',
  test_notification: '测试消息',
};

/** 本地脱敏：保留 origin + 路径首段，隐藏末尾 token（不依赖后端是否已脱敏） */
export function maskWebhookUrl(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (text.includes('****') || text.includes('***')) return text;
  try {
    const url = new URL(text);
    const segments = url.pathname.split('/').filter(Boolean);
    const last = segments[segments.length - 1] || '';
    const maskedTail = last.length > 6 ? `****${last.slice(-4)}` : '****';
    const prefix = segments.slice(0, -1).join('/');
    return `${url.origin}/${prefix ? `${prefix}/` : ''}${maskedTail}`;
  } catch {
    if (text.length <= 12) return '****';
    return `${text.slice(0, 8)}****${text.slice(-4)}`;
  }
}

export class NotifySettings {
  constructor(root = document, handlers = {}) {
    this.root = root;
    this.handlers = handlers;
    this._open = false;
    this._busy = false;
    this._status = null;
    this._lastError = '';
    this._lastFocus = null;
    this._onKeyDown = (event) => {
      if (event.key === 'Escape' && this._open) {
        event.preventDefault();
        this.close();
      }
    };
    this._ensureDom();
    this._bindEvents();
  }

  _ensureDom() {
    if (this.root.getElementById('notify-settings-overlay')) {
      this._refs();
      return;
    }
    const overlay = this.root.createElement('div');
    overlay.id = 'notify-settings-overlay';
    overlay.className = 'notify-overlay hidden';
    overlay.innerHTML = `
      <div class="notify-dialog" role="dialog" aria-modal="true" aria-labelledby="notify-settings-title">
        <div class="notify-dialog-head">
          <div class="notify-title-wrap">
            <span class="notify-title" id="notify-settings-title">飞书通知设置</span>
            <span class="notify-subtitle">Webhook · 告警 / 恢复通知</span>
          </div>
          <button type="button" class="close" id="notify-settings-close" title="关闭（Esc）">×</button>
        </div>
        <div class="notify-status-grid" id="notify-status-grid"></div>
        <div class="notify-result hidden" id="notify-settings-result"></div>
        <form class="notify-form" id="notify-settings-form" novalidate>
          <label class="notify-field notify-field-wide">
            <span>Webhook URL</span>
            <div class="notify-url-row">
              <input id="notify-webhook-url" type="password" autocomplete="new-password" spellcheck="false"
                placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." />
              <button type="button" class="btn small ghost" id="notify-webhook-toggle" aria-label="显示/隐藏 Webhook URL">显示</button>
            </div>
            <small class="notify-field-hint">留空则保持当前地址；URL 仅提交给后端，不回显、不打印到控制台。</small>
          </label>
          <div class="notify-form-grid">
            <label class="notify-field">
              <span>通知类型</span>
              <select id="notify-type">${TYPE_OPTIONS.map(
                ([value, label]) => `<option value="${value}">${label}</option>`,
              ).join('')}</select>
            </label>
            <label class="notify-field">
              <span>最低告警等级</span>
              <select id="notify-min-severity">
                <option value="critical">critical（严重）</option>
                <option value="warning">warning（警告）</option>
              </select>
            </label>
            <label class="notify-check">
              <input type="checkbox" id="notify-on-recovery" />
              <span>故障恢复时也发送通知</span>
            </label>
            <label class="notify-check">
              <input type="checkbox" id="notify-enabled" />
              <span>启用告警通知</span>
            </label>
          </div>
          <div class="notify-help">
            飞书群 → 设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook 地址；详见
            <code>campus-twin/docs/飞书机器人接入.md</code>。建议开启签名校验，URL 注意保密。
          </div>
        </form>
        <div class="notify-actions">
          <button type="button" class="btn ghost" id="notify-cancel">取消</button>
          <button type="button" class="btn ghost" id="notify-test-only">仅测试</button>
          <button type="button" class="btn" id="notify-save-test">保存并测试</button>
        </div>
      </div>`;
    this.root.body.appendChild(overlay);
    this._refs();
  }

  _refs() {
    const byId = (id) => this.root.getElementById(id);
    this.el = {
      overlay: byId('notify-settings-overlay'),
      dialog: this.root.querySelector('.notify-dialog'),
      close: byId('notify-settings-close'),
      statusGrid: byId('notify-status-grid'),
      result: byId('notify-settings-result'),
      form: byId('notify-settings-form'),
      url: byId('notify-webhook-url'),
      toggleUrl: byId('notify-webhook-toggle'),
      type: byId('notify-type'),
      minSeverity: byId('notify-min-severity'),
      onRecovery: byId('notify-on-recovery'),
      enabled: byId('notify-enabled'),
      cancel: byId('notify-cancel'),
      testOnly: byId('notify-test-only'),
      saveTest: byId('notify-save-test'),
    };
  }

  _bindEvents() {
    this.el.close?.addEventListener('click', () => this.close());
    this.el.cancel?.addEventListener('click', () => this.close());
    this.el.overlay?.addEventListener('click', (event) => {
      if (event.target === this.el.overlay) this.close();
    });
    this.el.form?.addEventListener('submit', (event) => event.preventDefault());
    this.el.saveTest?.addEventListener('click', () => this._handleSaveTest());
    this.el.testOnly?.addEventListener('click', () => this._handleTestOnly());
    this.el.toggleUrl?.addEventListener('click', () => {
      if (!this.el.url) return;
      const hidden = this.el.url.type === 'password';
      this.el.url.type = hidden ? 'text' : 'password';
      this.el.toggleUrl.textContent = hidden ? '隐藏' : '显示';
      this.el.toggleUrl.setAttribute('aria-label', hidden ? '隐藏 Webhook URL' : '显示 Webhook URL');
    });
  }

  get isOpen() {
    return this._open;
  }

  /** 打开对话框：status 为 normalizeNotifyStatus() 结果（可为 null） */
  open(status = null) {
    this._lastFocus = this.root.activeElement;
    this._lastError = '';
    this._setResult('');
    this.updateStatus(status, { syncForm: true });
    this.el.overlay?.classList.remove('hidden');
    this._open = true;
    document.addEventListener('keydown', this._onKeyDown);
    setTimeout(() => this.el.url?.focus(), 60);
    this.handlers.onChange?.(this.getDebug());
  }

  close() {
    if (!this._open) return;
    document.removeEventListener('keydown', this._onKeyDown);
    this.el.overlay?.classList.add('hidden');
    this._open = false;
    if (this._lastFocus && typeof this._lastFocus.focus === 'function') {
      try {
        this._lastFocus.focus();
      } catch {
        /* 关闭后焦点恢复失败不影响功能 */
      }
    }
    this.handlers.onChange?.(this.getDebug());
  }

  setBusy(busy, active = '') {
    this._busy = Boolean(busy);
    for (const button of [this.el.saveTest, this.el.testOnly, this.el.cancel]) {
      if (button) button.disabled = this._busy;
    }
    if (this.el.saveTest) {
      this.el.saveTest.textContent = this._busy && active === 'save' ? '保存中…' : '保存并测试';
    }
    if (this.el.testOnly) {
      this.el.testOnly.textContent = this._busy && active === 'test' ? '测试中…' : '仅测试';
    }
    this.handlers.onChange?.(this.getDebug());
  }

  /** 更新状态区与（可选）表单回填 */
  updateStatus(status, { syncForm = false } = {}) {
    this._status = status && typeof status === 'object' ? status : null;
    this._renderStatus();
    if (syncForm) this._fillForm();
    this.handlers.onChange?.(this.getDebug());
  }

  _maskedUrl() {
    return maskWebhookUrl(this._status?.webhookUrl || '');
  }

  _renderStatus() {
    const grid = this.el.statusGrid;
    if (!grid) return;
    const status = this._status;
    if (!status || !status.available) {
      grid.innerHTML = `<div class="notify-status-empty">通知服务未就绪（后端 /api/notify/status 不可用）</div>`;
      return;
    }
    const typeLabels = {
      feishu: '飞书',
      dingtalk: '钉钉',
      wecom: '企业微信',
      generic: '通用 Webhook',
      webhook: 'Webhook',
    };
    const enabledText = status.enabled ? '已启用' : '未启用';
    const lastSent = status.lastSentAt ? formatDateTime(status.lastSentAt) : '尚未发送';
    const lastError = status.lastError || '无';
    const lastKind =
      PAYLOAD_KIND_LABELS[status.lastPayloadKind] || status.lastPayloadKind || '—';
    const items = [
      ['启用状态', enabledText, status.enabled ? 'ok' : 'muted'],
      ['通知类型', typeLabels[status.type] || status.type || '—', 'muted'],
      ['最近发送', lastSent, 'muted'],
      ['最近错误', lastError, status.lastError ? 'bad' : 'ok'],
      ['成功 / 失败', `${status.sentCount || 0} / ${status.failedCount || 0}`, 'muted'],
      ['最近负载', lastKind, 'muted'],
      ['恢复通知', status.onRecovery ? '已开启' : '未开启', status.onRecovery ? 'ok' : 'muted'],
      ['Webhook', this._maskedUrl() || '未配置', 'muted'],
    ];
    grid.innerHTML = items
      .map(
        ([label, value, tone]) => `<div class="notify-status-item ${tone}">
          <span class="notify-status-label">${escapeHtml(label)}</span>
          <span class="notify-status-value" title="${escapeHtml(String(value))}">${escapeHtml(
            String(value),
          )}</span>
        </div>`,
      )
      .join('');
  }

  _fillForm() {
    const status = this._status;
    if (!this.el.form) return;
    const type = String(status?.type || 'feishu');
    const normalizedType = TYPE_OPTIONS.some(([value]) => value === type)
      ? type
      : type === 'webhook'
        ? 'generic'
        : 'feishu';
    if (this.el.type) this.el.type.value = normalizedType;
    if (this.el.minSeverity) {
      this.el.minSeverity.value = status?.minSeverity === 'warning' ? 'warning' : 'critical';
    }
    if (this.el.onRecovery) this.el.onRecovery.checked = Boolean(status?.onRecovery);
    if (this.el.enabled) this.el.enabled.checked = Boolean(status?.enabled);
    if (this.el.url) {
      this.el.url.value = '';
      this.el.url.type = 'password';
      if (this.el.toggleUrl) {
        this.el.toggleUrl.textContent = '显示';
        this.el.toggleUrl.setAttribute('aria-label', '显示 Webhook URL');
      }
      const masked = this._maskedUrl();
      this.el.url.placeholder = masked
        ? `已配置：${masked}（留空保持）`
        : 'https://open.feishu.cn/open-apis/bot/v2/hook/...';
    }
  }

  getFormValues() {
    const payload = {
      enabled: Boolean(this.el.enabled?.checked),
      webhook_type: this.el.type?.value || 'feishu',
      min_severity: this.el.minSeverity?.value || 'critical',
      on_recovery: Boolean(this.el.onRecovery?.checked),
    };
    const url = String(this.el.url?.value || '').trim();
    if (url) payload.webhook_url = url;
    return payload;
  }

  clearWebhookInput() {
    if (!this.el.url) return;
    this.el.url.value = '';
    this.el.url.type = 'password';
    if (this.el.toggleUrl) {
      this.el.toggleUrl.textContent = '显示';
      this.el.toggleUrl.setAttribute('aria-label', '显示 Webhook URL');
    }
  }

  _validate() {
    const url = String(this.el.url?.value || '').trim();
    const enabled = Boolean(this.el.enabled?.checked);
    const configured = Boolean(this._status?.configured || this._status?.webhookUrl);
    if (url && !/^https?:\/\/\S+$/i.test(url)) {
      this._setResult('Webhook URL 需要以 http:// 或 https:// 开头', 'error');
      return false;
    }
    if (enabled && !configured && !url) {
      this._setResult('启用通知前请填写 Webhook URL', 'error');
      return false;
    }
    return true;
  }

  _handleSaveTest() {
    if (this._busy) return;
    if (!this._validate()) return;
    this._lastError = '';
    this._setResult('');
    this.handlers.onSave?.(this.getFormValues());
  }

  _handleTestOnly() {
    if (this._busy) return;
    this._lastError = '';
    this._setResult('');
    this.handlers.onTest?.();
  }

  /** 显示内联结果；tone: info/ok/warn/error */
  _setResult(text, tone = '') {
    const node = this.el.result;
    if (!node) return;
    if (!text) {
      node.classList.add('hidden');
      node.textContent = '';
      node.classList.remove('info', 'ok', 'warn', 'error');
      return;
    }
    node.textContent = text;
    node.classList.remove('hidden', 'info', 'ok', 'warn', 'error');
    if (tone) node.classList.add(tone);
    this._lastError = tone === 'error' || tone === 'warn' ? text : '';
    this.handlers.onChange?.(this.getDebug());
  }

  setResult(text, tone = 'info') {
    this._setResult(text, tone);
  }

  setHint(text, tone = 'warn') {
    this._setResult(text, tone);
  }

  getDebug() {
    return {
      open: this._open,
      busy: this._busy,
      available: Boolean(this._status?.available),
      error: this._lastError,
    };
  }
}
