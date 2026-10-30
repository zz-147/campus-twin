/**
 * 数据归一化层（纯 JS，不依赖 three / DOM，可被 Node 校验脚本直接 import）。
 *
 * 后端接口字段以 snake_case 为主（见 backend/app/models.py），
 * 但任务文档里也出现过 device_id / mgmtIp 之类的驼峰别名。
 * 这里统一做防御式提取：字段缺失、命名漂移、类型变化都不会让页面崩溃，
 * 同时保证前端展示的每一项数据都能追溯到后端响应，绝不硬编码设备。
 */
import {
  fallbackSceneConfig,
  normalizeSceneConfig,
  normalizeScenePending,
} from '../scene/sceneConfig.js';

/** 设备状态四分类：与任务要求一致（Managed 绿 / Unreachable 红 / Unsupported 灰 / 无数据 蓝灰） */
export const STATUS_META = {
  managed: {
    label: '在线',
    en: 'Managed',
    css: '#38e08a',
    color: 0x38e08a,
    desc: '已纳管，采集正常',
  },
  unreachable: {
    label: '不可达',
    en: 'Unreachable',
    css: '#ff5266',
    color: 0xff5266,
    desc: '设备不可达 / 离线',
  },
  unsupported: {
    label: '不支持',
    en: 'Unsupported',
    css: '#8b97a8',
    color: 0x8b97a8,
    desc: '设备不支持纳管采集',
  },
  unknown: {
    label: '无数据',
    en: '无数据',
    css: '#6f8fb8',
    color: 0x6f8fb8,
    desc: '暂无采集数据',
  },
};

const STATUS_KEYS = ['managed', 'unreachable', 'unsupported', 'unknown'];

function pick(source, ...keys) {
  if (!source || typeof source !== 'object') return null;
  for (const key of keys) {
    const value = source[key];
    if (value !== null && value !== undefined && value !== '') return value;
  }
  return null;
}

function str(value) {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

function lower(value) {
  return str(value).toLowerCase();
}

function num(value) {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * 修复“二次编码”文本：后端 SQLite 里历史告警等字段可能把 UTF-8 字节按 Latin-1 存过，
 * 展示前尝试还原；真实中文（含 >0xFF 字符）与正常英文会原样返回。
 */
export function repairText(value) {
  if (typeof value !== 'string' || value === '') return value ?? '';
  // 含 Latin-1 以外的字符说明本来就是正常文本（中文 / emoji），直接返回
  if ([...value].some((ch) => ch.charCodeAt(0) > 0xff)) return value;
  if (!/[\u00c0-\u00ff]/.test(value)) return value;
  try {
    const bytes = Uint8Array.from([...value].map((ch) => ch.charCodeAt(0) & 0xff));
    const decoded = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    return decoded || value;
  } catch {
    // 不是合法的 UTF-8 字节序列 → 认为是正常含重音的文本，保持原样
    return value;
  }
}

/**
 * 状态归类。
 * 优先级：不可达 > 不支持 > 已纳管 > 主机 ping 结果 > 默认无数据。
 * 设备取 collectionStatus / reachability；主机只有 pingStatus，也复用该函数。
 */
export function deriveStatusKind({ collectionStatus, reachability, pingStatus } = {}) {
  const rs = lower(reachability);
  const cs = lower(collectionStatus);
  const ps = lower(pingStatus);

  if (/(unreachable|not.?reachable|offline|failed|\bdown\b)/.test(rs)) return 'unreachable';
  if (/(unsupported|not.?supported|unmanaged|unmonitored)/.test(cs)) return 'unsupported';
  if (/(managed|collected|monitored|active)/.test(cs)) return 'managed';
  if (/(unreachable|offline|failed|\bdown\b)/.test(ps)) return 'unreachable';
  if (/(success|\bup\b|reachable|online)/.test(ps)) return 'managed';
  if (/(reachable|\bup\b|success|ok|active)/.test(rs)) return 'managed';
  if (/(unsupported|unmanaged)/.test(rs)) return 'unsupported';
  return 'unknown';
}

function statusFields(statusKind) {
  const meta = STATUS_META[statusKind] || STATUS_META.unknown;
  return { statusKind, statusMeta: meta };
}

/** 解析时间：兼容 epoch 秒/毫秒、ISO 字符串、PT 的 "YYYY-MM-DD HH:mm:ss"（按本地时间） */
export function parseTimestamp(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return value > 1e12 ? value / 1000 : value;
  }
  const text = String(value).trim();
  if (!text) return null;
  const numeric = Number(text);
  if (Number.isFinite(numeric)) return numeric > 1e12 ? numeric / 1000 : numeric;
  const parsed = Date.parse(text.replace(' ', 'T'));
  if (!Number.isNaN(parsed)) return parsed / 1000;
  return null;
}

function stableId(prefix, ...parts) {
  const raw = parts.map((part) => str(part)).join('|');
  let hash = 0;
  for (let i = 0; i < raw.length; i += 1) {
    hash = (hash * 31 + raw.charCodeAt(i)) >>> 0;
  }
  return `${prefix}-${hash.toString(16)}`;
}

// --------------------------------------------------------------------------- //
// 实体归一化
// --------------------------------------------------------------------------- //

/** 受管网络设备（/api/devices 的 devices 列表） */
export function normalizeDevice(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const reachability = str(pick(raw, 'reachability', 'reachabilityStatus'));
  const collectionStatus = str(pick(raw, 'collection_status', 'collectionStatus'));
  const hostname =
    str(pick(raw, 'hostname', 'hostName', 'deviceName', 'name')) ||
    str(pick(raw, 'management_ip', 'managementIp', 'mgmtIp', 'managementIpAddress')) ||
    str(pick(raw, 'id', 'device_id', 'deviceId')) ||
    '未知设备';
  return {
    id: str(pick(raw, 'id', 'device_id', 'deviceId', 'instanceUuid')) || stableId('device', hostname),
    hostname,
    managementIp: str(pick(raw, 'management_ip', 'managementIp', 'mgmtIp', 'managementIpAddress', 'ipAddress')),
    mac: str(pick(raw, 'mac_address', 'macAddress', 'mac')),
    serialNumber: str(pick(raw, 'serial_number', 'serialNumber')),
    platformId: str(pick(raw, 'platform_id', 'platformId', 'platform')),
    productId: str(pick(raw, 'product_id', 'productId', 'product')),
    deviceType: str(pick(raw, 'device_type', 'deviceType', 'type')),
    family: str(pick(raw, 'family')),
    role: str(pick(raw, 'role')),
    softwareVersion: str(pick(raw, 'software_version', 'softwareVersion', 'osVersion')),
    upTime: str(pick(raw, 'up_time', 'upTime', 'uptime')),
    interfaceCount: num(pick(raw, 'interface_count', 'interfaceCount')),
    reachability,
    collectionStatus,
    source: str(pick(raw, 'source')) || 'netcon',
    lastUpdated: pick(raw, 'last_updated', 'lastUpdated', 'lastUpdateTime'),
    ...statusFields(deriveStatusKind({ collectionStatus, reachability })),
  };
}

/** 终端主机（/api/hosts 的 hosts 列表：PC / Server 等） */
export function normalizeHost(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const hostname =
    str(pick(raw, 'hostname', 'hostName', 'name')) ||
    str(pick(raw, 'ip', 'hostIp', 'ipAddress')) ||
    '未知主机';
  const pingStatus = str(pick(raw, 'ping_status', 'pingStatus', 'status'));
  return {
    id: str(pick(raw, 'id', 'host_id', 'hostId')) || stableId('host', hostname, pick(raw, 'mac', 'hostMac')),
    hostname,
    ip: str(pick(raw, 'ip', 'hostIp', 'ipAddress')),
    mac: str(pick(raw, 'mac', 'hostMac', 'hostMacAddress', 'macAddress')),
    connectedDeviceName: str(pick(raw, 'connected_device_name', 'connectedDeviceName', 'connectedNetworkDeviceName')),
    connectedDeviceId: str(pick(raw, 'connected_device_id', 'connectedDeviceId', 'connectedNetworkDeviceId')),
    connectedInterface: str(pick(raw, 'connected_interface', 'connectedInterfaceName', 'interfaceName')),
    hostType: str(pick(raw, 'host_type', 'hostType', 'type')),
    pingStatus,
    vlanId: str(pick(raw, 'vlan_id', 'vlanId', 'vlan')),
    source: str(pick(raw, 'source')) || 'netcon',
    lastUpdated: pick(raw, 'last_updated', 'lastUpdated'),
    ...statusFields(deriveStatusKind({ pingStatus })),
  };
}

/** 网络健康问题（/api/issues 的 issues 列表） */
export function normalizeIssue(raw = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const name = repairText(str(pick(raw, 'issue_name', 'issueName', 'name'))) || '健康问题';
  const description = repairText(str(pick(raw, 'issue_description', 'issueDescription', 'description')));
  const sourceName = repairText(
    str(pick(raw, 'issue_source', 'issueSource', 'sourceName', 'deviceName', 'device_id')),
  );
  const sourceTime = pick(raw, 'issue_timestamp', 'issueTimestamp', 'timestamp', 'first_seen');
  // W3 时间基准：observed_at 是宿主机采集时间（展示用），source_ts 是 NC 原始时间（审计用）
  const observedAt =
    parseTimestamp(pick(raw, 'observed_at', 'observedAt')) ?? parseTimestamp(sourceTime);
  return {
    id:
      str(pick(raw, 'issue_id', 'issueId', 'id')) ||
      stableId('issue', name, description, sourceName),
    name,
    description,
    sourceName,
    severity: lower(pick(raw, 'severity')) === 'critical' ? 'critical' : 'warning',
    status: str(pick(raw, 'status')) || 'active',
    source: str(pick(raw, 'source')) || 'netcon',
    rawTime: str(sourceTime),
    sourceTs: str(pick(raw, 'source_ts', 'sourceTs')) || str(sourceTime),
    observedAt,
    staleSource: Boolean(pick(raw, 'stale_source', 'staleSource')),
    ts: observedAt,
  };
}

/** 告警（/api/alerts 历史 + WebSocket 实时推送） */
export function normalizeAlert(raw = {}, origin = 'history') {
  if (!raw || typeof raw !== 'object') return null;
  const title = repairText(str(pick(raw, 'title', 'alert_type', 'alertType'))) || '告警';
  const dedupKey = str(pick(raw, 'dedup_key', 'dedupKey'));
  const id = str(pick(raw, 'id'));
  const severity = lower(pick(raw, 'severity')) || 'warning';
  const stateRaw = lower(pick(raw, 'state', 'status'));
  const state =
    stateRaw === 'active' ||
    stateRaw === 'cleared' ||
    stateRaw === 'info' ||
    stateRaw === 'silenced'
      ? stateRaw
      : severity === 'info'
        ? 'info'
        : 'active';
  const observedAt = parseTimestamp(
    pick(raw, 'observed_at', 'observedAt', 'created_at', 'createdAt', 'timestamp'),
  );
  return {
    key: `alert:${dedupKey || id || stableId('alert', title, pick(raw, 'device_id'))}`,
    alertId: pick(raw, 'id', 'alert_id', 'alertId'),
    alertType: str(pick(raw, 'alert_type', 'alertType')),
    severity,
    title,
    detail: repairText(str(pick(raw, 'detail', 'description'))),
    deviceId: repairText(str(pick(raw, 'device_id', 'deviceId'))),
    linkId: str(pick(raw, 'link_id', 'linkId')),
    issueId: str(pick(raw, 'issue_id', 'issueId')),
    state,
    resolvedAt: parseTimestamp(pick(raw, 'resolved_at', 'resolvedAt')),
    clearReason: repairText(str(pick(raw, 'clear_reason', 'clearReason'))),
    staleSource: Boolean(pick(raw, 'stale_source', 'staleSource')),
    acknowledged: Boolean(pick(raw, 'acknowledged')),
    observedAt,
    sourceTs: str(pick(raw, 'source_ts', 'sourceTs')),
    ts: observedAt,
    deletedAt: parseTimestamp(pick(raw, 'deleted_at', 'deletedAt')),
    origin,
  };
}

/** 告警生命周期统计（/api/alerts/summary 或 /api/alerts 响应内的 summary） */
export function normalizeAlertsSummary(raw, alerts = []) {
  const list = Array.isArray(alerts) ? alerts : [];
  const countState = (state) => list.filter((alert) => alert?.state === state).length;
  const active = num(pick(raw, 'active')) ?? countState('active');
  const cleared = num(pick(raw, 'cleared')) ?? countState('cleared');
  const info = num(pick(raw, 'info')) ?? countState('info');
  const total = num(pick(raw, 'total')) ?? active + cleared + info;
  return { active, cleared, info, total };
}

/** W3 检测引擎目录（/api/detection/rules 或 /api/health.detection） */
export function normalizeDetection(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const rules = Array.isArray(raw.rules) ? raw.rules : [];
  return {
    engine: str(pick(raw, 'engine')) || 'self-rules-v1',
    rules: rules
      .map((rule) => (typeof rule === 'string' ? rule : str(pick(rule, 'rule_id', 'ruleId', 'id'))))
      .filter(Boolean),
    notes: Array.isArray(raw.notes) ? raw.notes.map((note) => repairText(String(note))) : [],
  };
}

/** W10：Webhook 告警通知状态（GET /api/notify/status；404 时返回 null → 显示“未配置”） */
export function normalizeNotifyStatus(raw) {
  if (!raw || typeof raw !== 'object') return null;
  return {
    available: true,
    enabled: Boolean(pick(raw, 'enabled')),
    type: str(pick(raw, 'type', 'webhook_type', 'webhookType')) || null,
    minSeverity: str(pick(raw, 'min_severity', 'minSeverity')) || null,
    lastSentAt: parseTimestamp(pick(raw, 'last_sent_at', 'lastSentAt')),
    lastError: repairText(str(pick(raw, 'last_error', 'lastError'))),
    sentCount: num(pick(raw, 'sent_count', 'sentCount')) ?? 0,
    webhookUrl: str(pick(raw, 'url', 'webhook_url', 'webhookUrl')),
  };
}

/** 网络健康总览（/api/network-health 的 network_health） */
export function normalizeHealth(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const total = num(pick(raw, 'total_devices', 'totalDevices', 'numNetworkDevices'));
  const healthy = num(pick(raw, 'healthy_devices', 'healthyDevices'));
  let percent = num(
    pick(
      raw,
      'healthy_devices_percentage',
      'healthyDevicesPercentage',
      'health_percentage',
      'healthPercentage',
      'totalPercentage',
    ),
  );
  if (percent === null && total && total > 0 && healthy !== null) {
    percent = (healthy / total) * 100;
  }
  return {
    percent,
    clientsPercent: num(
      pick(raw, 'healthy_clients_percentage', 'healthyClientsPercentage', 'healthyClient'),
    ),
    totalDevices: total,
    healthyDevices: healthy,
    unhealthyDevices: num(pick(raw, 'unhealthy_devices', 'unhealthyDevices')),
    unreachableDevices: num(pick(raw, 'unreachable_devices', 'unreachableDevices')) ?? 0,
    source: str(pick(raw, 'source')),
  };
}

function isLinkUp(status) {
  const text = lower(status);
  if (!text) return true; // 后端未提供状态时按“未知但存在”处理
  if (/(\bdown\b|disabled|fail|error|0)/.test(text)) return false;
  return true;
}

/** 物理拓扑（/api/topology） */
export function normalizeTopology(raw) {
  const nodes = Array.isArray(raw?.nodes) ? raw.nodes : [];
  const links = Array.isArray(raw?.links) ? raw.links : [];
  return {
    nodes: nodes
      .filter((node) => node && typeof node === 'object')
      .map((node) => ({
        id: str(pick(node, 'id', 'nodeId', 'deviceId')),
        x: num(pick(node, 'x', 'positionX')),
        y: num(pick(node, 'y', 'positionY')),
        label: str(pick(node, 'label', 'hostname', 'name')),
      }))
      .filter((node) => node.id),
    links: links
      .filter((link) => link && typeof link === 'object')
      .map((link) => {
        const source = str(pick(link, 'source', 'startNodeId', 'sourceNodeId', 'from'));
        const target = str(pick(link, 'target', 'endNodeId', 'targetNodeId', 'to'));
        const linkStatus = str(pick(link, 'link_status', 'linkStatus', 'status'));
        return {
          id: str(pick(link, 'id', 'linkId')) || `${source}->${target}`,
          source,
          target,
          startPortName: str(pick(link, 'start_port_name', 'startPortName', 'sourcePort')),
          endPortName: str(pick(link, 'end_port_name', 'endPortName', 'targetPort')),
          linkStatus,
          up: isLinkUp(linkStatus),
        };
      })
      .filter((link) => link.source && link.target),
  };
}

/**
 * NC 数据源控制器（场景里“中心机房”的那个 NC 标记）。
 * 只有后端健康检查明确报告 NC 可达时才显示；它是数据源指示器，不是虚构设备。
 */
function normalizeController(healthStatus, bundle) {
  const driver = str(pick(healthStatus, 'driver')) || str(pick(bundle, 'driver'));
  const driverStatus = healthStatus?.driver_status || null;
  const reachable = driverStatus?.reachable === true;
  return {
    available: Boolean(healthStatus) && reachable,
    online: reachable,
    driver,
    datasetSource: str(pick(healthStatus, 'dataset_source')) || str(pick(bundle, 'source')),
    baseUrl: str(pick(driverStatus, 'base_url', 'baseUrl')),
    lastOkAt: num(pick(driverStatus, 'last_ok_at', 'lastOkAt')),
  };
}

// --------------------------------------------------------------------------- //
// 快照聚合
// --------------------------------------------------------------------------- //

/**
 * 把一个原始数据包归一化成页面使用的快照模型。
 * bundle 的字段来源：
 *  - REST：/api/devices、/api/hosts、/api/issues、/api/topology、/api/network-health、/api/health、/api/alerts
 *  - WebSocket：首帧 {"type":"snapshot","data":{...}} 中的 data
 */
export function normalizeSnapshot(bundle = {}) {
  const devices = (Array.isArray(bundle.devices) ? bundle.devices : [])
    .map(normalizeDevice)
    .filter(Boolean);
  const hostsBase = (Array.isArray(bundle.hosts) ? bundle.hosts : [])
    .map(normalizeHost)
    .filter(Boolean);
  const issues = (Array.isArray(bundle.issues) ? bundle.issues : [])
    .map(normalizeIssue)
    .filter(Boolean);
  const alerts = (Array.isArray(bundle.alerts) ? bundle.alerts : [])
    .map((alert) => normalizeAlert(alert, 'history'))
    .filter(Boolean);
  const topology = normalizeTopology(bundle.topology);
  const health = normalizeHealth(bundle.networkHealth);
  const healthStatus = bundle.healthStatus || null;
  const source = str(bundle.source) || str(pick(healthStatus, 'dataset_source')) || '';
  const stale = Boolean(bundle.stale ?? pick(healthStatus, 'stale') ?? false);
  const capturedAt =
    bundle.capturedAt ?? pick(healthStatus, 'captured_at') ?? Date.now() / 1000;
  const capturedAtIso =
    bundle.capturedAtIso ?? pick(healthStatus, 'server_time') ?? null;

  // ---- W3 实时数据增强：宿主机观测时间 / 快照新鲜度 / 告警生命周期 ----
  // hosts_observed_at 是 /api/hosts 与 /api/health 里的“宿主机采集时间”；
  // snapshot_age_seconds 是后端在响应时刻算好的延迟，收到后由前端继续按秒推进。
  const hostsObservedAt =
    num(bundle.hostsObservedAt) ??
    parseTimestamp(pick(healthStatus, 'hosts_observed_at', 'hostsObservedAt')) ??
    null;
  const hostsObservedAtIso = bundle.hostsObservedAtIso ?? null;
  const observedAt = num(bundle.observedAt) ?? parseTimestamp(capturedAtIso) ?? capturedAt;
  // 后端 REST 会直接给 snapshot_age_seconds；WebSocket 快照没有该字段时，
  // 用宿主机观测时间在本地换算，保证状态栏始终能显示“数据更新于 X 秒前”。
  const snapshotAgeSeconds =
    num(bundle.snapshotAgeSeconds) ??
    (hostsObservedAt ? Math.max(0, Date.now() / 1000 - Number(hostsObservedAt)) : null);
  // 主机记录附带观测时间（展示“这条主机数据是什么时候观测到的”）
  const hosts = hostsBase.map((host) => ({
    ...host,
    observedAt: hostsObservedAt ?? observedAt,
    snapshotAgeSeconds,
  }));
  const alertsSummary = normalizeAlertsSummary(bundle.alertsSummary ?? healthStatus?.detection?.alert_states, alerts);
  const detection =
    normalizeDetection(bundle.detection) ??
    (healthStatus?.detection ? normalizeDetection(healthStatus.detection) : null);
  const freshness = {
    observedAt: hostsObservedAt ?? observedAt,
    observedAtIso: hostsObservedAtIso ?? capturedAtIso,
    snapshotAgeSeconds,
    receivedAt: Date.now(),
    stale,
  };

  // ---- W9：配置驱动场景（GET /api/scene；接口不可用时回退默认 4 栋楼） ----
  const sceneConfig = normalizeSceneConfig(bundle.scene) || fallbackSceneConfig();
  const pendingFromScene = normalizeScenePending(bundle.scenePending);
  const pendingMap = new Map(
    sceneConfig.pendingDevices.map((device) => [device.name.toLowerCase(), device]),
  );
  for (const device of pendingFromScene) {
    if (!pendingMap.has(device.name.toLowerCase())) pendingMap.set(device.name.toLowerCase(), device);
  }
  const pendingDevices = [...pendingMap.values()];

  const devicesOnline = devices.filter((device) => device.statusKind === 'managed').length;
  const hostsOnline = hosts.filter((host) => host.statusKind === 'managed').length;

  return {
    devices,
    hosts,
    issues,
    alerts,
    alertsSummary,
    detection,
    topology,
    health,
    freshness,
    sceneConfig,
    pendingDevices,
    notifyStatus: normalizeNotifyStatus(bundle.notifyStatus),
    sceneMeta: {
      version: sceneConfig.version,
      source: sceneConfig.source,
      theme: sceneConfig.theme,
      counts: sceneConfig.counts,
      pending: pendingDevices.length,
    },
    collect: {
      stale,
      driver: str(pick(healthStatus, 'driver')),
      datasetSource: source,
      metricsSource: str(pick(healthStatus, 'metrics_source')),
      lastSuccessAt: pick(healthStatus, 'last_success_at'),
      lastPollAt: pick(healthStatus, 'last_poll_at'),
      lastError: str(pick(healthStatus, 'last_error')),
      collectErrors: num(pick(healthStatus, 'collect_errors')),
      consecutiveFailures: num(pick(healthStatus, 'consecutive_failures')),
      pollInterval: num(pick(healthStatus, 'poll_interval')),
      polls: num(pick(healthStatus, 'polls')),
      websocketClients: num(pick(healthStatus, 'websocket_clients')),
    },
    controller: normalizeController(healthStatus, bundle),
    summary: {
      devicesTotal: devices.length,
      devicesOnline,
      hostsTotal: hosts.length,
      hostsOnline,
      issuesTotal: issues.length,
      // W7.1：区分真正的活跃 issue 与 NC 滞后（stale_source）issue
      issuesActive: issues.filter(
        (issue) => !issue.staleSource && !['cleared', 'info'].includes(String(issue.status || '').toLowerCase()),
      ).length,
      issuesStale: issues.filter((issue) => issue.staleSource).length,
      alertsActive: alertsSummary.active,
      alertsCleared: alertsSummary.cleared,
      alertsTotal: alertsSummary.total,
      // W9：配置驱动场景统计
      buildingsTotal: sceneConfig.buildings.length,
      pendingDevicesTotal: pendingDevices.length,
      sceneVersion: sceneConfig.version,
      sceneSource: sceneConfig.source,
      healthPercent: health?.percent ?? null,
      capturedAt,
      capturedAtIso,
    },
    source,
    stale,
    capturedAt,
    capturedAtIso,
    demo: Boolean(bundle.demo),
    receivedAt: Date.now(),
  };
}

export const STATUS_ORDER = STATUS_KEYS;
