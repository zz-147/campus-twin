/**
 * 告警列表视图模型（纯函数，无 DOM，可被浏览器与 Node 冒烟脚本共用）。
 *
 * W7.1 语义修正（核心）：
 * - 「活跃」只认两种来源：
 *     1) `GET /api/alerts?state=active` 的 active alerts；
 *     2) `GET /api/issues` 中 **stale_source=false** 的活跃 issue；
 * - NC 滞后 issue（`stale_source=true`）表示 NC 自身的问题记录还没清除，但宿主机数据源
 *   实际状态已经恢复（后端按 host.ping_status / device.reachabilityStatus 判定）：
 *   - 不计入活跃告警数；
 *   - 不触发 3D 红色标记 / 脉冲光环；
 *   - 列表中展示为「已恢复 · NC 记录滞后」的次要状态（灰黄、不抢眼）；
 *   - 时间使用 observed_at（宿主机采集时间），source_ts 放 tooltip 供审计。
 */

/** stale_source issue 的次要状态文案 */
export const STALE_SOURCE_ISSUE_LABEL = '已恢复 · NC 记录滞后';

const STALE_SOURCE_REASON = 'NC 问题记录滞后，数据源实际状态已恢复（stale_source）';

/** issue 是否应被视为「活跃」（stale_source / cleared / info 都不是活跃） */
export function issueIsActive(issue) {
  if (!issue) return false;
  if (issue.staleSource) return false;
  const status = String(issue.status || 'active').toLowerCase();
  return status !== 'cleared' && status !== 'info';
}

/** 列表/详情共用的状态文案（stale_source 优先显示为“已恢复 · NC 记录滞后”） */
export function alertStateLabel(entry = {}) {
  if (entry.staleSource) return STALE_SOURCE_ISSUE_LABEL;
  switch (entry.state || 'active') {
    case 'active':
      return '活跃';
    case 'cleared':
      return '已恢复';
    case 'info':
      return '信息';
    case 'silenced':
      return '已静默';
    default:
      return String(entry.state || '未知');
  }
}

/** 活跃条数：只统计真正的 active（stale_source 的 issue 不算活跃） */
export function countActiveEntries(entries = []) {
  return entries.filter((entry) => entry.state === 'active' && !entry.staleSource).length;
}

/**
 * 把 issues + alerts + 实时告警合并成告警列表条目。
 *
 * @param {object} snapshot normalizeSnapshot() 的快照
 * @param {Array} liveAlerts WebSocket 实时告警（normalizeAlert 结果）
 * @param {object} options
 * @param {(deviceId: string) => string} [options.resolveDeviceName]
 *        告警里的 device_id 是设备/主机 UUID，可传入映射函数转成主机名（浏览器侧使用）
 * @returns {Array} 按「活跃优先、观测时间倒序」排序的条目
 */
export function buildAlertEntries(snapshot, liveAlerts = [], { resolveDeviceName = null } = {}) {
  if (!snapshot) return [];
  const entries = [];
  const issueIds = new Set();

  // 用 cleared alert 的 clear_reason / resolved_at 补充 stale issue 的恢复信息
  const alertsByIssueId = new Map();
  for (const alert of snapshot.alerts || []) {
    if (!alert.issueId) continue;
    const existing = alertsByIssueId.get(alert.issueId);
    if (!existing || (alert.resolvedAt || 0) > (existing.resolvedAt || 0)) {
      alertsByIssueId.set(alert.issueId, alert);
    }
  }

  // 1) NC 原生健康问题（含 stale_source=true 的滞后记录）
  for (const issue of snapshot.issues || []) {
    const stale = Boolean(issue.staleSource);
    const active = issueIsActive(issue);
    issueIds.add(issue.id);
    const matchedAlert = stale ? alertsByIssueId.get(issue.id) : null;
    entries.push({
      key: `issue:${issue.id}`,
      device: issue.sourceName,
      title: issue.name,
      description: issue.description,
      severity: issue.severity,
      state: active ? 'active' : 'cleared',
      sourceType: 'issue',
      alertId: null,
      alertType: 'issue',
      staleSource: stale,
      staleReason: stale ? STALE_SOURCE_REASON : '',
      time: issue.observedAt ?? issue.ts ?? issue.rawTime,
      observedAt: issue.observedAt ?? issue.ts,
      sourceTs: issue.sourceTs,
      resolvedAt: matchedAlert?.resolvedAt ?? null,
      clearReason: matchedAlert?.clearReason ?? '',
      origin: 'issue',
    });
  }

  // 2) 检测引擎告警（active / cleared；info 一次性事件不算告警）
  // 历史告警按 alert id 保留每一条事件（同一 dedup_key 的多次故障/恢复都应可见）；
  // 实时告警仍按 dedup_key 去重，避免 WS 重复推送刷屏。
  const pushAlert = (alert, { history = false } = {}) => {
    if (!alert) return;
    if (alert.state === 'info' || alert.severity === 'info') return;
    // 与健康问题重复（同一 issue 的 cleared 告警由上面的 issue 条目代表）
    if (alert.issueId && issueIds.has(alert.issueId)) return;
    const entryKey =
      history && alert.alertId !== null && alert.alertId !== undefined && alert.alertId !== ''
        ? `alert-history:${alert.alertId}`
        : alert.key;
    if (entries.some((entry) => entry.key === entryKey)) return;
    let deviceId = alert.deviceId || '';
    if (typeof resolveDeviceName === 'function') {
      deviceId = resolveDeviceName(deviceId) || deviceId;
    }
    entries.push({
      key: entryKey,
      device: deviceId,
      title: alert.title,
      description: alert.detail,
      severity: alert.severity,
      state:
        alert.state === 'cleared'
          ? 'cleared'
          : alert.state === 'silenced'
            ? 'silenced'
            : 'active',
      sourceType: 'alert',
      alertId: alert.alertId ?? null,
      alertType: alert.alertType || '',
      staleSource: Boolean(alert.staleSource),
      staleReason: alert.staleSource ? STALE_SOURCE_REASON : '',
      time: alert.observedAt ?? alert.ts,
      observedAt: alert.observedAt ?? alert.ts,
      sourceTs: alert.sourceTs,
      resolvedAt: alert.resolvedAt,
      clearReason: alert.clearReason,
      origin: 'alert',
    });
  };
  for (const alert of snapshot.alerts || []) pushAlert(alert, { history: true });
  for (const alert of liveAlerts) pushAlert(alert);

  // 活跃告警优先，其次按观测时间倒序
  entries.sort((a, b) => {
    const aActive = a.state === 'active' ? 1 : 0;
    const bActive = b.state === 'active' ? 1 : 0;
    if (aActive !== bActive) return bActive - aActive;
    return (Number(b.time) || 0) - (Number(a.time) || 0);
  });
  return entries.slice(0, 60);
}
