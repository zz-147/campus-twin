/**
 * 场景条目构建（纯 JS，无 three.js 依赖）。
 *
 * W9：楼栋/设备位置优先来自配置驱动的 `sceneConfig`（GET /api/scene，由后端维护），
 * 后端接口不可用时回退 layout.js 的默认 4 栋楼 + 原有启发式落位。
 *
 * 条目来源：
 *   1. 库存设备 /api/devices + 主机 /api/hosts（状态、告警、详情都以它们为准）
 *   2. 配置里存在、库存里还没有的设备（例如自动发现后 pending 的新设备）
 *   3. NC 数据源控制器（/api/health driver_status）
 *
 * 配置只决定“画在哪”，不决定“是否存在”；不存在于库存与配置的设备绝不会被渲染。
 */
import { STATUS_META } from '../api/normalize.js';
import { BUILDINGS, computeSlot, guessBuildingId, sortItemsForLayout } from './layout.js';
import {
  SCENE_BUILDING_KEYWORDS,
  fallbackSceneConfig,
  findConfigBuilding,
  inferConfigBuilding,
} from './sceneConfig.js';

/** 标记类型 → 中文名（用于详情面板与悬浮提示） */
export const MARKER_TYPE_LABELS = {
  switch: '交换机',
  router: '路由器',
  pc: 'PC / 终端',
  server: '服务器',
  ap: '无线 AP',
  camera: '摄像头',
  controller: 'NC 数据源控制器',
  unknown: '网络设备',
};

const CONFIG_TYPE_ALIASES = {
  switch: 'switch',
  'multi-layer switch': 'switch',
  multilayerswitch: 'switch',
  router: 'router',
  pc: 'pc',
  client: 'pc',
  terminal: 'pc',
  server: 'server',
  ap: 'ap',
  'access point': 'ap',
  accesspoint: 'ap',
  camera: 'camera',
  cam: 'camera',
  controller: 'controller',
  nc: 'controller',
  unknown: 'unknown',
};

function lower(value) {
  return String(value ?? '').trim().toLowerCase();
}

function finiteNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 根据设备字段判断用哪种几何体：交换机=小盒子、路由器=圆柱、PC=小方块、服务器=机架条 */
function detectDeviceMarker(device) {
  const text = [
    device.hostname,
    device.deviceType,
    device.family,
    device.platformId,
    device.productId,
  ]
    .join(' ')
    .toLowerCase();
  if (/(router|isr|4331|2911|1941|2901)/.test(text)) return 'router';
  if (/(camera|cam-)/.test(text)) return 'camera';
  if (/(access.?point|\bap\b|wireless|wifi)/.test(text)) return 'ap';
  if (/(switch|3650|2960|3560|3750|multilayer)/.test(text)) return 'switch';
  return 'unknown';
}

function detectHostMarker(host) {
  const text = [host.hostname, host.hostType].join(' ').toLowerCase();
  if (/(server|srv)/.test(text)) return 'server';
  if (/(pc|client|terminal|laptop)/.test(text)) return 'pc';
  return 'pc';
}

function normalizeMarkerType(type, fallbackType) {
  const key = lower(type).replace(/[_-]+/g, ' ').trim();
  return CONFIG_TYPE_ALIASES[key] || CONFIG_TYPE_ALIASES[lower(type)] || fallbackType || 'unknown';
}

function indexConfigDevices(sceneConfig) {
  const map = new Map();
  for (const building of sceneConfig?.buildings || []) {
    for (const device of building.devices || []) {
      map.set(lower(device.name), { ...device, buildingId: device.buildingId || building.id });
    }
  }
  return map;
}

/**
 * @param {object} snapshot normalizeSnapshot() 的结果（含 sceneConfig）
 * @param {Array} liveAlerts WebSocket 实时告警
 * @returns {Array} 场景条目（按楼栋分组、已落位、带告警/新鲜度/新设备标记）
 */
export function buildSceneItems(snapshot, liveAlerts = []) {
  if (!snapshot) return [];

  const sceneConfig = snapshot.sceneConfig || fallbackSceneConfig();
  const configBuildings = (sceneConfig.buildings || []).length
    ? sceneConfig.buildings
    : fallbackSceneConfig().buildings;
  const configDeviceByName = indexConfigDevices(sceneConfig);
  const inventory = new Set();

  const rawItems = [];

  // ---- 1. 受管网络设备（/api/devices） ----
  for (const device of snapshot.devices || []) {
    const name = lower(device.hostname);
    inventory.add(name);
    const configDevice = configDeviceByName.get(name) || null;
    rawItems.push({
      kind: 'device',
      id: device.id,
      hostname: device.hostname,
      label: device.hostname,
      type: normalizeMarkerType(configDevice?.type, detectDeviceMarker(device)),
      statusKind: device.statusKind,
      statusMeta: device.statusMeta || STATUS_META.unknown,
      device,
      host: null,
      controller: null,
      configDevice,
      sceneStatus: lower(configDevice?.status) || 'confirmed',
      pending: false,
      alert: false,
      alertIssues: [],
    });
  }

  // ---- 2. 终端主机（/api/hosts） ----
  for (const host of snapshot.hosts || []) {
    const name = lower(host.hostname);
    inventory.add(name);
    const configDevice = configDeviceByName.get(name) || null;
    rawItems.push({
      kind: 'host',
      id: host.id,
      hostname: host.hostname,
      label: host.hostname,
      type: normalizeMarkerType(configDevice?.type, detectHostMarker(host)),
      statusKind: host.statusKind,
      statusMeta: host.statusMeta || STATUS_META.unknown,
      device: null,
      host,
      controller: null,
      configDevice,
      sceneStatus: lower(configDevice?.status) || 'confirmed',
      pending: false,
      alert: false,
      alertIssues: [],
    });
  }

  // ---- 3. 配置里存在、库存里还没有的设备（自动发现 / pending 新设备） ----
  // 注意：场景配置里的 NC 控制器由下面的“数据源控制器”条目统一渲染，避免重复。
  for (const configDevice of configDeviceByName.values()) {
    const name = lower(configDevice.name);
    if (inventory.has(name)) continue;
    if (normalizeMarkerType(configDevice.type, '') === 'controller') continue;
    rawItems.push({
      kind: 'device',
      id: configDevice.id || `scene:${configDevice.name}`,
      hostname: configDevice.name,
      label: configDevice.name,
      type: normalizeMarkerType(configDevice.type, 'unknown'),
      statusKind: 'unknown',
      statusMeta: STATUS_META.unknown,
      device: null,
      host: null,
      controller: null,
      configDevice,
      sceneStatus: lower(configDevice.status) || 'confirmed',
      pending: true,
      configOnly: true,
      alert: false,
      alertIssues: [],
    });
  }

  // ---- 4. NC 数据源控制器（优先复用场景配置里的 controller 条目） ----
  const configController = [...configDeviceByName.values()].find(
    (device) => normalizeMarkerType(device.type, '') === 'controller',
  );
  if (snapshot.controller?.available) {
    const controllerLabel =
      configController && configController.name && configController.name !== 'NC'
        ? configController.name
        : 'NC 控制器';
    rawItems.push({
      kind: 'controller',
      id: configController?.id || 'nc-controller',
      hostname: 'NC 控制器',
      label: controllerLabel,
      type: 'controller',
      statusKind: snapshot.controller.online ? 'managed' : 'unknown',
      statusMeta: snapshot.controller.online ? STATUS_META.managed : STATUS_META.unknown,
      device: null,
      host: null,
      controller: snapshot.controller,
      configDevice: configController || null,
      sceneStatus: 'confirmed',
      pending: false,
      alert: false,
      alertIssues: [],
    });
  }

  // ---- 5. 楼栋归属：配置直给 → 主机名规则 → 接入交换机 → 拓扑邻居 → 首栋楼兜底 ----
  const legacyCore = () =>
    findConfigBuilding(sceneConfig, 'core') ||
    configBuildings.find((building) => /机房|core/i.test(`${building.name} ${building.id}`)) ||
    configBuildings[0];
  const mapLegacyBuilding = (legacyId) => findConfigBuilding(sceneConfig, legacyId);
  const inferByHostname = (hostname) =>
    inferConfigBuilding(sceneConfig, {
      hostname,
      keywordMap: SCENE_BUILDING_KEYWORDS,
    }) || mapLegacyBuilding(guessBuildingId(hostname));

  for (const item of rawItems) {
    let building = null;
    if (item.configDevice?.buildingId) {
      building = findConfigBuilding(sceneConfig, item.configDevice.buildingId);
    }
    if (!building) building = inferByHostname(item.hostname);
    if (!building && item.host?.connectedDeviceName) {
      building = inferByHostname(item.host.connectedDeviceName);
    }
    if (!building && item.kind === 'controller') building = legacyCore();
    item.building = building || null;
    item.fallbackBuilding = !building;
  }

  const links = snapshot.topology?.links || [];
  const byDeviceId = new Map(
    rawItems.filter((item) => item.kind === 'device' && item.device).map((item) => [item.id, item]),
  );
  for (const item of rawItems) {
    if (!item.fallbackBuilding || item.kind !== 'device') continue;
    const link = links.find((candidate) => candidate.source === item.id || candidate.target === item.id);
    if (!link) continue;
    const peerId = link.source === item.id ? link.target : link.source;
    const peer = byDeviceId.get(peerId);
    if (peer?.building) {
      item.building = peer.building;
      item.fallbackBuilding = false;
    }
  }
  // 无楼栋可归的 pending 设备先不进 3D（由“新设备提示”让用户选择楼栋）
  for (const item of rawItems) {
    if (!item.building) item.building = legacyCore();
  }

  // ---- 6. 告警匹配（W7.1 语义：active alerts + 非 stale 的活跃 issue） ----
  const hostnameById = new Map();
  for (const item of rawItems) {
    const id = lower(item.id);
    if (id) hostnameById.set(id, lower(item.hostname));
  }
  const alertNames = new Set();
  const issuesByName = new Map();
  const activeAlertsByName = new Map();
  const pushByName = (map, name, entry) => {
    if (!map.has(name)) map.set(name, []);
    map.get(name).push(entry);
  };

  for (const issue of snapshot.issues || []) {
    const name = lower(issue.sourceName);
    if (!name) continue;
    const stale = Boolean(issue.staleSource);
    const active = !stale && !['cleared', 'info'].includes(lower(issue.status || 'active'));
    if (active) alertNames.add(name);
    pushByName(issuesByName, name, {
      name: issue.name,
      description: issue.description,
      severity: issue.severity,
      state: active ? 'active' : 'cleared',
      staleSource: stale,
      time: issue.observedAt ?? issue.ts,
      sourceTs: issue.sourceTs,
    });
  }

  const resolveAlertHostname = (alert) => {
    const direct = hostnameById.get(lower(alert.deviceId));
    if (direct) return direct;
    const dedupKey = lower(alert.dedupKey);
    const separator = dedupKey.indexOf(':');
    if (separator > 0) {
      const mapped = hostnameById.get(dedupKey.slice(separator + 1));
      if (mapped) return mapped;
    }
    const title = lower(alert.title);
    for (const item of rawItems) {
      if (item.hostname && title.includes(lower(item.hostname))) return lower(item.hostname);
    }
    return null;
  };

  const applyActiveAlert = (alert) => {
    if (!alert) return;
    if (
      alert.state === 'cleared' ||
      alert.state === 'info' ||
      alert.state === 'silenced' ||
      alert.severity === 'info'
    ) {
      return;
    }
    const name = resolveAlertHostname(alert);
    if (!name) return;
    alertNames.add(name);
    const bucket = activeAlertsByName.get(name) || [];
    if (bucket.some((entry) => entry.key === alert.key)) return;
    bucket.push({
      key: alert.key,
      name: alert.title,
      description: alert.detail,
      severity: alert.severity,
      state: alert.state || 'active',
      time: alert.observedAt ?? alert.ts,
      sourceTs: alert.sourceTs,
    });
    activeAlertsByName.set(name, bucket);
  };
  for (const alert of snapshot.alerts || []) applyActiveAlert(alert);
  for (const alert of liveAlerts || []) applyActiveAlert(alert);

  // ---- 7. 按楼栋分组、排序、落位 ----
  const items = [];
  for (const building of configBuildings) {
    const group = rawItems.filter((item) => item.building?.id === building.id);
    group.sort(sortItemsForLayout);

    group.forEach((item, index) => {
      const name = lower(item.hostname);
      item.key = `${item.kind}:${item.id}`;
      item.building = building;
      item.buildingId = building.id;
      item.buildingName = building.name;
      item.pending = item.sceneStatus === 'pending';
      const configPosition = item.configDevice?.position || null;
      const px = finiteNumber(configPosition?.x);
      const pz = finiteNumber(configPosition?.z);
      const py = finiteNumber(configPosition?.y);
      if (px !== null && pz !== null) {
        item.position = {
          x: px,
          y: py !== null ? py : building.height + 3.8,
          z: pz,
        };
        item.customPosition = true;
      } else {
        item.position = computeSlot(building, index, group.length);
      }
      item.labelOffsetY = index % 2 === 0 ? 4.6 : 9.8;
      item.alert = alertNames.has(name);
      item.alertIssues = issuesByName.get(name) || [];
      item.alertItems = [...(issuesByName.get(name) || []), ...(activeAlertsByName.get(name) || [])];
      item.observedAt =
        item.kind === 'host'
          ? item.host?.observedAt ?? snapshot.freshness?.observedAt ?? snapshot.capturedAt
          : snapshot.freshness?.observedAt ?? snapshot.capturedAt;
      item.snapshotAgeSeconds =
        item.host?.snapshotAgeSeconds ?? snapshot.freshness?.snapshotAgeSeconds ?? null;
      item.typeLabel = MARKER_TYPE_LABELS[item.type] || MARKER_TYPE_LABELS.unknown;
      item.statusLabel = (item.statusMeta || STATUS_META.unknown).label;
      items.push(item);
    });
  }

  return items;
}

/** 统计信息（自测/调试用，不参与渲染） */
export function summarizeSceneItems(items = [], sceneConfig = null) {
  const byBuilding = {};
  for (const item of items) {
    byBuilding[item.buildingName] = (byBuilding[item.buildingName] || 0) + 1;
  }
  const buildingIds = new Set(items.map((item) => item.buildingId));
  const configBuildingCount = sceneConfig?.buildings?.length;
  return {
    total: items.length,
    devices: items.filter((item) => item.kind === 'device').length,
    hosts: items.filter((item) => item.kind === 'host').length,
    controllers: items.filter((item) => item.kind === 'controller').length,
    alerted: items.filter((item) => item.alert).length,
    pending: items.filter((item) => item.pending).length,
    buildings: configBuildingCount ?? (buildingIds.size || BUILDINGS.length),
    byBuilding,
  };
}
