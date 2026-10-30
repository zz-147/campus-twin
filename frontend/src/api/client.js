/**
 * 后端数据接入客户端。
 *
 * 数据通道：
 * 1) REST 轮询：/api/health、/api/devices、/api/topology、/api/hosts、
 *    /api/issues、/api/network-health、/api/alerts —— 首屏与兜底快照；
 * 2) WebSocket /ws：首帧 snapshot + 采集轮次的快照/告警推送，负责“实时”；
 * 3) 断线自动重连（指数退避），期间降级为 REST 轮询，后端恢复后自动切回。
 *
 * 连接状态通过 onConnection 上报，main.js 据此显示“已连接 / 未连接”。
 */
import { normalizeAlert, normalizeSnapshot } from './normalize.js';

const ENV_BASE = import.meta.env?.VITE_API_BASE;
/** 后端地址：默认本机 FastAPI；可用 VITE_API_BASE 覆盖（构建时注入） */
export const API_BASE = String(ENV_BASE || 'http://127.0.0.1:8000').replace(/\/+$/, '');

/** 带超时的 JSON 请求 */
async function fetchJson(url, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}：${url}`);
    }
    return await response.json();
  } catch (error) {
    if (error?.name === 'AbortError') throw new Error(`请求超时（${timeoutMs}ms）：${url}`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

const ENDPOINTS = {
  health: '/api/health',
  devices: '/api/devices',
  topology: '/api/topology',
  hosts: '/api/hosts',
  issues: '/api/issues',
  networkHealth: '/api/network-health',
  // W3：告警带生命周期（active/cleared/info），summary 给状态栏计数
  alerts: '/api/alerts?limit=50&state=all',
  alertsSummary: '/api/alerts/summary',
  detection: '/api/detection/rules',
  // W9：配置驱动场景（后端未就绪时 404 → 前端回退默认 4 栋楼布局）
  scene: '/api/scene',
  scenePending: '/api/scene/pending',
  // W10：Webhook 告警通知状态（404 时通知区显示“未配置”）
  notifyStatus: '/api/notify/status',
};

/** 场景配置轮询间隔（自动发现新设备/新楼栋，无需刷新页面） */
const SCENE_POLL_INTERVAL_MS = 4000;
const SCENE_POLL_SLOW_MS = 20000;

export class DataClient {
  constructor(handlers = {}) {
    this.handlers = handlers;
    this.apiBase = API_BASE;
    this.wsUrl = `${API_BASE.replace(/^http/i, 'ws')}/ws`;
    this.online = false;
    this.wsOpen = false;

    this._ws = null;
    this._wsRetry = 0;
    this._wsTimer = null;
    this._pollTimer = null;
    this._heartbeatTimer = null;
    this._refreshing = false;
    this._stopped = false;
    this._lastError = '';
    this._healthStatus = null;
    this._historyAlerts = [];
    this._alertsSummary = null;
    this._detection = null;
    this._refreshAlertSyncTimer = null;
    // W9：场景配置版本轮询（自动发现新设备/新楼栋）
    this._sceneDoc = null;
    this._scenePendingDoc = null;
    this._sceneVersion = null;
    this._scenePollTimer = null;
    this._scenePollAvailable = true;
    this._notifyStatus = null;

    this._onVisibilityChange = () => {
      if (document.visibilityState === 'visible') this.refresh();
    };
    document.addEventListener('visibilitychange', this._onVisibilityChange);
  }

  start() {
    if (this._stopped) return;
    this.refresh();
    this._connectWs();
    this._startHeartbeat();
    this._schedulePoll(5000);
    this._scheduleScenePoll(SCENE_POLL_INTERVAL_MS);
    return this;
  }

  stop() {
    this._stopped = true;
    document.removeEventListener('visibilitychange', this._onVisibilityChange);
    clearTimeout(this._wsTimer);
    clearTimeout(this._pollTimer);
    clearTimeout(this._refreshAlertSyncTimer);
    clearTimeout(this._scenePollTimer);
    clearInterval(this._heartbeatTimer);
    if (this._ws) {
      try {
        this._ws.close();
      } catch {
        /* 忽略关闭异常 */
      }
      this._ws = null;
    }
  }

  // ------------------------------------------------------------------ //
  // REST 快照
  // ------------------------------------------------------------------ //
  async refresh() {
    if (this._refreshing || this._stopped) return;
    this._refreshing = true;
    const names = Object.keys(ENDPOINTS);
    try {
      const results = await Promise.allSettled(
        names.map((name) => fetchJson(this.apiBase + ENDPOINTS[name])),
      );
      const docs = {};
      names.forEach((name, index) => {
        docs[name] = results[index].status === 'fulfilled' ? results[index].value : null;
      });

      // 设备快照是页面渲染的硬依赖：拿不到就视为后端不可用（其它接口允许局部失败）
      if (!docs.devices) {
        const reason = results[names.indexOf('devices')]?.reason;
        throw new Error(reason?.message || '后端未返回设备数据');
      }

      this._healthStatus = docs.health || this._healthStatus;
      if (docs.alerts?.alerts) this._historyAlerts = docs.alerts.alerts;
      // 缓存 W3 增强字段，供 WS 快照合并时复用
      this._alertsSummary = docs.alertsSummary || docs.alerts?.summary || this._alertsSummary;
      this._detection = docs.detection || docs.health?.detection || this._detection;
      // W9：场景配置（404 时保持 null → normalize 回退默认布局）
      this._sceneDoc = docs.scene || this._sceneDoc;
      this._scenePendingDoc = docs.scenePending || this._scenePendingDoc;
      if (docs.scene?.version !== undefined) this._sceneVersion = docs.scene.version;
      // W10：通知状态（404 时保持 null → 显示“未配置”）
      if (docs.notifyStatus) this._notifyStatus = docs.notifyStatus;

      const bundle = {
        devices: docs.devices?.devices || [],
        hosts: docs.hosts?.hosts || [],
        issues: docs.issues?.issues || [],
        topology: docs.topology || { nodes: [], links: [] },
        networkHealth: docs.networkHealth?.network_health ?? null,
        healthStatus: this._healthStatus,
        alerts: this._historyAlerts,
        alertsSummary: this._alertsSummary ?? null,
        detection: this._detection ?? null,
        scene: this._sceneDoc ?? null,
        scenePending: this._scenePendingDoc ?? null,
        notifyStatus: this._notifyStatus ?? null,
        capturedAt: docs.devices?.captured_at ?? null,
        capturedAtIso: docs.devices?.captured_at_iso ?? null,
        observedAt: docs.devices?.observed_at ?? docs.health?.captured_at ?? null,
        hostsObservedAt: docs.hosts?.observed_at ?? null,
        hostsObservedAtIso: docs.hosts?.observed_at_iso ?? null,
        snapshotAgeSeconds: docs.hosts?.snapshot_age_seconds ?? null,
        source: docs.devices?.source ?? docs.health?.dataset_source ?? null,
        stale: Boolean(docs.devices?.stale),
      };

      this._lastError = '';
      this._setOnline(true);
      this.handlers.onSnapshot?.(normalizeSnapshot(bundle));
    } catch (error) {
      this._lastError = error?.message || String(error);
      this._setOnline(false);
    } finally {
      this._refreshing = false;
    }
  }

  _setOnline(online) {
    const changed = online !== this.online;
    this.online = online;
    this.handlers.onConnection?.({
      online,
      changed,
      wsOpen: this.wsOpen,
      apiBase: this.apiBase,
      error: online ? '' : this._lastError,
    });
  }

  _schedulePoll(delay) {
    if (this._stopped) return;
    clearTimeout(this._pollTimer);
    const nextDelay = delay ?? (this.wsOpen ? 15000 : 5000);
    this._pollTimer = setTimeout(async () => {
      await this.refresh();
      this._schedulePoll();
    }, nextDelay);
  }

  // ------------------------------------------------------------------ //
  // WebSocket 实时通道
  // ------------------------------------------------------------------ //
  _connectWs() {
    if (this._stopped || typeof WebSocket === 'undefined') return;
    let socket;
    try {
      socket = new WebSocket(this.wsUrl);
    } catch (error) {
      this._lastError = `WebSocket 初始化失败：${error?.message || error}`;
      this._scheduleWsRetry();
      return;
    }
    this._ws = socket;

    socket.onopen = () => {
      this.wsOpen = true;
      this._wsRetry = 0;
      this._lastError = '';
      // WS 首帧会推 snapshot；同时刷新一次 REST 以补齐 /api/health 的采集状态
      this.refresh();
    };

    socket.onmessage = (event) => this._handleWsMessage(event.data);

    socket.onclose = () => {
      this.wsOpen = false;
      this._ws = null;
      this._scheduleWsRetry();
      // WS 断开不代表 REST 不可用（例如代理限制）；主动刷新一次以校准连接状态
      this.refresh();
    };

    socket.onerror = () => {
      // 具体原因由 onclose 统一处理，避免重复上报
    };
  }

  _scheduleWsRetry() {
    if (this._stopped || this._wsTimer) return;
    const delay = Math.min(1000 * 2 ** Math.min(this._wsRetry, 4), 15000);
    this._wsRetry += 1;
    this._wsTimer = setTimeout(() => {
      this._wsTimer = null;
      this._connectWs();
    }, delay);
  }

  _startHeartbeat() {
    clearInterval(this._heartbeatTimer);
    this._heartbeatTimer = setInterval(() => {
      if (this.wsOpen && this._ws?.readyState === WebSocket.OPEN) {
        try {
          this._ws.send('ping');
        } catch {
          /* 发送失败等待 onclose 重连 */
        }
      }
    }, 20000);
  }

  _handleWsMessage(raw) {
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      return; // 非 JSON 帧直接忽略
    }
    if (!message || typeof message !== 'object') return;

    if (message.type === 'snapshot' && message.data) {
      const data = message.data;
      const previous = this._healthStatus || {};
      // WS 快照不含完整 health 字段，这里合并最新采集状态，避免 REST 数据被旧值覆盖
      this._healthStatus = {
        ...previous,
        stale: Boolean(data.stale),
        driver: data.driver || previous.driver,
        dataset_source: data.source || previous.dataset_source,
        counts: data.counts || previous.counts,
        hosts_observed_at: data.hosts_observed_at ?? previous.hosts_observed_at,
      };
      const bundle = {
        devices: data.devices || [],
        hosts: data.hosts || [],
        issues: data.issues || [],
        topology: data.topology || { nodes: [], links: [] },
        networkHealth: data.network_health ?? null,
        healthStatus: this._healthStatus,
        alerts: this._historyAlerts,
        alertsSummary: this._alertsSummary ?? this._healthStatus?.detection?.alert_states ?? null,
        detection: this._detection ?? this._healthStatus?.detection ?? null,
        capturedAt: data.captured_at ?? null,
        capturedAtIso: data.captured_at_iso ?? null,
        observedAt: data.observed_at ?? null,
        hostsObservedAt: data.hosts_observed_at ?? null,
        hostsObservedAtIso: data.hosts_observed_at_iso ?? null,
        snapshotAgeSeconds: null, // WS 快照未带延迟，前端按 observed_at 自行计算
        source: data.source ?? null,
        stale: Boolean(data.stale),
        // W9：若后端把场景配置并入 WS 快照则直接使用，否则沿用 REST 缓存
        scene: data.scene ?? this._sceneDoc ?? null,
        scenePending: data.scene_pending ?? this._scenePendingDoc ?? null,
        // W10：WS 快照不带通知状态，沿用 REST 缓存（否则会被覆盖成“未配置”）
        notifyStatus: this._notifyStatus ?? null,
      };
      this._lastError = '';
      this._setOnline(true);
      this.handlers.onSnapshot?.(normalizeSnapshot(bundle));
      return;
    }

    if (message.type === 'alert' && message.data) {
      // 实时告警：立即进告警列表并触发对应设备脉冲高亮
      this.handlers.onAlert?.(normalizeAlert(message.data, 'live'));
      this._scheduleAlertSync();
      return;
    }

    if (message.type === 'alerts_cleared' && message.data) {
      // W3：恢复事件聚合成一条摘要推送（不再逐条刷屏）
      this.handlers.onAlertsCleared?.(message.data);
      this._scheduleAlertSync();
      return;
    }

    if (message.type === 'status' && message.data) {
      this.handlers.onStatus?.(message.data);
    }
  }

  /** 收到告警/恢复事件后短暂延迟刷新 REST，校准告警生命周期状态（防抖） */
  _scheduleAlertSync() {
    if (this._stopped || this._refreshAlertSyncTimer) return;
    this._refreshAlertSyncTimer = setTimeout(() => {
      this._refreshAlertSyncTimer = null;
      this.refresh();
    }, 1200);
  }

  // ------------------------------------------------------------------ //
  // W9：场景配置轮询（自动发现新设备/新楼栋，无需刷新页面）
  // ------------------------------------------------------------------ //
  _scheduleScenePoll(delay) {
    if (this._stopped) return;
    clearTimeout(this._scenePollTimer);
    this._scenePollTimer = setTimeout(async () => {
      await this._pollSceneVersion();
      this._scheduleScenePoll(
        this._scenePollAvailable ? SCENE_POLL_INTERVAL_MS : SCENE_POLL_SLOW_MS,
      );
    }, Math.max(500, delay));
  }

  async _pollSceneVersion() {
    if (this._stopped) return;
    try {
      const scene = await fetchJson(`${this.apiBase}${ENDPOINTS.scene}`, 6000);
      const pending = await fetchJson(`${this.apiBase}${ENDPOINTS.scenePending}`, 6000).catch(
        () => null,
      );
      this._scenePollAvailable = true;
      const previousVersion = this._sceneVersion;
      const previousPending = JSON.stringify(this._scenePendingDoc?.devices || this._scenePendingDoc || []);
      const nextPending = JSON.stringify(pending?.devices || pending || []);
      const versionChanged = scene?.version !== previousVersion;
      const pendingChanged = nextPending !== previousPending;
      this._sceneDoc = scene;
      this._scenePendingDoc = pending;
      if (scene?.version !== undefined) this._sceneVersion = scene.version;
      // 场景版本或 pending 列表变化时触发一次完整刷新，让 3D 场景/提示自动更新
      if (versionChanged || pendingChanged) this.refresh();
    } catch {
      // 后端 W9 场景接口还未就绪（404）或暂不可用：降低轮询频率，不影响其它功能
      this._scenePollAvailable = false;
    }
  }
}
