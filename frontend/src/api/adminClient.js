/**
 * W9 数据管理客户端：告警删除/清空/静默/撤销、场景配置确认/删除/新增、审计查询。
 *
 * 所有写操作统一带 `X-Operator: web-ui` 头（后端审计用），接口路径遵循
 * docs/W9-Agent扩展设计.md；后端 W9 接口未就绪时抛 `kind='unsupported'` 的门槛错误，
 * 由 UI 提示“后端暂不支持”，不影响其它功能。
 */
import { API_BASE } from './client.js';

const OPERATOR = 'web-ui';

export class AdminError extends Error {
  constructor(message, { kind = 'unknown', status = null, detail = '' } = {}) {
    super(message);
    this.name = 'AdminError';
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

async function requestJson(url, { method = 'GET', body = null, timeoutMs = 8000, operator = OPERATOR } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method,
      cache: 'no-store',
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        'X-Operator': operator,
        ...(body !== null && body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(body !== null && body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      let detail = '';
      if (payload && typeof payload.detail === 'string') detail = payload.detail;
      else if (payload && typeof payload.message === 'string') detail = payload.message;
      const kind =
        response.status === 404
          ? 'unsupported'
          : response.status === 409
            ? 'conflict'
            : response.status === 422
              ? 'invalid'
              : response.status >= 500
                ? 'server'
                : 'http';
      throw new AdminError(
        `后端接口返回 ${response.status}${detail ? `：${detail}` : ''}`,
        { kind, status: response.status, detail },
      );
    }
    return payload ?? {};
  } catch (error) {
    if (error instanceof AdminError) throw error;
    if (error?.name === 'AbortError') throw new AdminError('请求超时', { kind: 'timeout' });
    throw new AdminError(`后端未连接：${error?.message || error}`, { kind: 'offline' });
  } finally {
    clearTimeout(timer);
  }
}

export class AdminClient {
  constructor({ baseUrl = API_BASE, operator = OPERATOR } = {}) {
    this.baseUrl = String(baseUrl || API_BASE).replace(/\/+$/, '');
    this.operator = operator;
  }

  _request(path, options = {}) {
    return requestJson(`${this.baseUrl}${path}`, { ...options, operator: this.operator });
  }

  // ---- 告警数据管理（不依赖 Agent） ----
  deleteAlert(alertId) {
    return this._request(`/api/alerts/${encodeURIComponent(alertId)}`, { method: 'DELETE' });
  }

  clearClearedAlerts({ before = null } = {}) {
    return this._request('/api/alerts/clear', {
      method: 'POST',
      body: before ? { scope: 'cleared', before } : { scope: 'cleared' },
    });
  }

  silenceAlert(alertId) {
    return this._request(`/api/alerts/${encodeURIComponent(alertId)}/silence`, {
      method: 'POST',
      body: {},
    });
  }

  restoreAlert(alertId) {
    return this._request(`/api/alerts/${encodeURIComponent(alertId)}/restore`, {
      method: 'POST',
      body: {},
    });
  }

  fetchAudit(limit = 20) {
    return this._request(`/api/audit?limit=${encodeURIComponent(limit)}`);
  }

  // ---- 场景配置管理 ----
  fetchScene() {
    return this._request('/api/scene');
  }

  fetchPendingScene() {
    return this._request('/api/scene/pending');
  }

  addBuilding(building) {
    return this._request('/api/scene/buildings', { method: 'POST', body: building });
  }

  upsertBuildingDevice(buildingId, device) {
    return this._request(
      `/api/scene/buildings/${encodeURIComponent(buildingId)}/devices`,
      { method: 'POST', body: device },
    );
  }

  confirmSceneDevice(name) {
    return this._request(`/api/scene/devices/${encodeURIComponent(name)}/confirm`, {
      method: 'POST',
      body: {},
    });
  }

  removeSceneDevice(name) {
    return this._request(`/api/scene/devices/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    });
  }

  // ---- W10：路径追踪 / 告警根因分组 / Webhook 通知 ----
  /** NC flow-analysis 可能要轮询 30s，前端超时放宽到 40s */
  pathTrace(sourceIp, destIp, { timeoutMs = 40000 } = {}) {
    return this._request('/api/path-trace', {
      method: 'POST',
      body: { source_ip: sourceIp, dest_ip: destIp },
      timeoutMs,
    });
  }

  alertGroups(state = 'active') {
    return this._request(`/api/alerts/groups?state=${encodeURIComponent(state)}`);
  }

  notifyStatus() {
    return this._request('/api/notify/status');
  }

  notifyTest() {
    return this._request('/api/notify/test', { method: 'POST', body: {}, timeoutMs: 20000 });
  }

  resetScene() {
    return this._request('/api/scene/reset', { method: 'POST', body: {} });
  }
}

export { API_BASE };
