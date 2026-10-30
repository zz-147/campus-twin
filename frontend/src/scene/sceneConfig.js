/**
 * 配置驱动的 3D 场景模型（纯 JS，无 three.js 依赖）。
 *
 * W9：场景不再写死在前端 layout.js，而是由后端 `GET /api/scene` 提供：
 *   { version, theme, buildings:[{ id,name,position:{x,z},size:{width,depth,height},
 *     style:{color}, status, devices:[{name,type,position,status,...}] }], pending:[...] }
 *
 * - 后端不可用 / 接口 404 时回退到 layout.js 的默认 4 栋楼（既有演示不受影响）；
 * - 新楼栋/新设备位置来自配置；`status=pending` 的设备由前端浅色 + 「新设备」角标提示；
 * - 本模块只做数据归一化，不含任何写死的真实设备清单。
 */
import { BUILDINGS } from './layout.js';

export const SCENE_SOURCE_API = 'api';
export const SCENE_SOURCE_FALLBACK = 'fallback';

const BUILDING_PALETTE = [0x2f6f9e, 0x2a8a7e, 0x7a63b6, 0x9a783f, 0x4e8f6b, 0x8a5f8f, 0x5f7f9a];

function num(value, fallback = 0) {
  if (value === null || value === undefined || value === '') return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function str(value) {
  return value === null || value === undefined ? '' : String(value).trim();
}

function lower(value) {
  return str(value).toLowerCase();
}

function parseColor(value, fallback) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const text = str(value);
  if (/^#?[0-9a-f]{6}$/i.test(text)) return parseInt(text.replace(/^#/, ''), 16);
  if (/^0x[0-9a-f]{1,6}$/i.test(text)) return parseInt(text.slice(2), 16);
  return fallback;
}

/** 兼容 position:{x,y,z} / position:[x,y,z] / {x,z} 三种写法 */
function normalizePosition(raw) {
  if (!raw) return null;
  if (Array.isArray(raw)) {
    if (raw.length < 2) return null;
    return { x: num(raw[0]), y: raw.length > 2 ? num(raw[1]) : null, z: num(raw[raw.length - 1]) };
  }
  if (typeof raw === 'object') {
    return { x: num(raw.x ?? raw[0]), y: raw.y === undefined ? null : num(raw.y), z: num(raw.z ?? raw[2] ?? raw.y) };
  }
  return null;
}

function normalizeSceneDevice(raw, buildingId, index) {
  if (!raw || typeof raw !== 'object') return null;
  const name = str(raw.name ?? raw.hostname ?? raw.device_name ?? raw.deviceName ?? raw.hostName);
  if (!name) return null;
  const position =
    normalizePosition(raw.position) ??
    (raw.x !== undefined || raw.z !== undefined
      ? { x: num(raw.x), y: raw.y === undefined ? null : num(raw.y), z: num(raw.z) }
      : null);
  return {
    id: str(raw.id ?? raw.device_id ?? raw.deviceId) || `scene:${buildingId}:${name}:${index}`,
    name,
    type: lower(raw.type ?? raw.device_type ?? raw.deviceType ?? raw.kind),
    status: lower(raw.status) || 'confirmed',
    buildingId: str(raw.building_id ?? raw.buildingId) || buildingId || '',
    inferredBuilding: str(raw.inferred_building ?? raw.inferredBuilding ?? raw.building_name),
    position,
    model: str(raw.model ?? raw.product ?? raw.platform ?? raw.productId),
    ip: str(raw.ip ?? raw.management_ip ?? raw.mgmtIp ?? raw.managementIp),
    source: str(raw.source) || 'scene-config',
  };
}

function normalizeBuilding(raw, index) {
  if (!raw || typeof raw !== 'object') return null;
  const id = str(raw.id ?? raw.building_id ?? raw.buildingId) || `building-${index + 1}`;
  const name = str(raw.name ?? raw.label ?? raw.title) || `楼栋 ${index + 1}`;
  const position =
    normalizePosition(raw.position) ??
    (raw.x !== undefined || raw.z !== undefined
      ? { x: num(raw.x), y: null, z: num(raw.z) }
      : { x: 0, y: null, z: 0 });
  const size = raw.size && typeof raw.size === 'object' ? raw.size : {};
  const width = Math.max(12, num(size.width ?? raw.width, 60));
  const depth = Math.max(12, num(size.depth ?? raw.depth, 30));
  const height = Math.max(8, num(size.height ?? raw.height, 20));
  const style = raw.style && typeof raw.style === 'object' ? raw.style : {};
  const color = parseColor(style.color ?? raw.color, BUILDING_PALETTE[index % BUILDING_PALETTE.length]);
  const rawDevices = Array.isArray(raw.devices) ? raw.devices : [];
  const devices = rawDevices
    .map((device, deviceIndex) => normalizeSceneDevice(device, id, deviceIndex))
    .filter(Boolean);
  return {
    id,
    name,
    x: position.x,
    z: position.z,
    width,
    depth,
    height,
    color,
    status: lower(raw.status) || lower(style.status) || 'confirmed',
    style,
    devices,
  };
}

/** 从 /api/scene/pending 的多种可能形态里提取 pending 列表 */
export function normalizeScenePending(raw) {
  if (!raw) return [];
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (Array.isArray(raw.devices)) list = raw.devices;
  else if (Array.isArray(raw.pending)) list = raw.pending;
  else if (Array.isArray(raw.items)) list = raw.items;
  return list
    .filter((item) => item && typeof item === 'object')
    .map((item, index) => {
      const name = str(item.name ?? item.hostname ?? item.device_name ?? item.deviceName);
      if (!name) return null;
      const buildingPosition =
        item.position && typeof item.position === 'object'
          ? normalizePosition(item.position)
          : null;
      return {
        id: str(item.id) || `pending:${name}:${index}`,
        name,
        type: lower(item.type ?? item.device_type ?? item.deviceType),
        status: 'pending',
        buildingId: str(item.building_id ?? item.buildingId ?? item.inferred_building_id),
        inferredBuilding: str(
          item.inferred_building ?? item.inferredBuilding ?? item.building_name ?? item.buildingName,
        ),
        model: str(item.model ?? item.product ?? item.platform),
        ip: str(item.ip ?? item.management_ip ?? item.mgmtIp),
        source: str(item.source) || 'auto-discovery',
        position: buildingPosition,
      };
    })
    .filter(Boolean);
}

/**
 * 归一化 GET /api/scene 响应。结构不合法时返回 null（调用方回退默认布局）。
 */
export function normalizeSceneConfig(raw, { receivedAt = Date.now() } = {}) {
  if (!raw || typeof raw !== 'object') return null;
  const rawBuildings = Array.isArray(raw.buildings) ? raw.buildings : null;
  if (!rawBuildings || rawBuildings.length === 0) return null;
  const buildings = rawBuildings.map(normalizeBuilding).filter(Boolean);
  if (!buildings.length) return null;

  const devices = [];
  for (const building of buildings) {
    for (const device of building.devices) devices.push({ ...device, buildingId: building.id });
  }
  // pending 可能在顶层 /api/scene 响应里，也可能只在 /api/scene/pending 里
  const pendingList = normalizeScenePending(raw.pending ?? raw.pending_devices ?? raw.pendingDevices);
  const pendingByName = new Map(pendingList.map((device) => [lower(device.name), device]));
  for (const device of devices) {
    if (device.status === 'pending' && !pendingByName.has(lower(device.name))) {
      pendingByName.set(lower(device.name), { ...device, source: 'scene-config' });
    }
  }
  const pendingDevices = [...pendingByName.values()];

  return {
    version: num(raw.version, 0),
    theme: str(raw.theme) || 'campus-lowpoly',
    source: SCENE_SOURCE_API,
    receivedAt,
    buildings,
    devices,
    pendingDevices,
    counts: {
      buildings: buildings.length,
      devices: devices.length,
      pending: pendingDevices.length,
    },
  };
}

/** 默认回退场景：沿用 layout.js 的 4 栋楼（无配置设备，设备走原有启发式落位） */
export function fallbackSceneConfig() {
  return {
    version: 0,
    theme: 'fallback',
    source: SCENE_SOURCE_FALLBACK,
    receivedAt: 0,
    buildings: BUILDINGS.map((building) => ({
      ...building,
      status: 'confirmed',
      style: {},
      devices: [],
    })),
    devices: [],
    pendingDevices: [],
    counts: { buildings: BUILDINGS.length, devices: 0, pending: 0 },
  };
}

/** 楼栋集合签名：位置/尺寸/名称变化时触发 3D 重建 */
export function buildingSignature(buildings = []) {
  return JSON.stringify(
    buildings.map((building) => [
      building.id,
      building.name,
      Number(building.x),
      Number(building.z),
      Number(building.width),
      Number(building.depth),
      Number(building.height),
      Number(building.color),
      building.status || 'confirmed',
    ]),
  );
}

/** 在配置里查找楼栋（id 或中文名/关键字匹配） */
export function findConfigBuilding(config, idOrName) {
  if (!config || !Array.isArray(config.buildings)) return null;
  const target = lower(idOrName);
  if (!target) return null;
  return (
    config.buildings.find(
      (building) =>
        lower(building.id) === target ||
        lower(building.name) === target ||
        lower(building.name).includes(target) ||
        target.includes(lower(building.name)),
    ) || null
  );
}

/** 给设备推断配置中的楼栋：先配置直给，再按主机名关键字匹配楼栋 id/名称 */
export function inferConfigBuilding(config, { buildingId, hostname, keywordMap = {} } = {}) {
  if (!config) return null;
  if (buildingId) {
    const matched = findConfigBuilding(config, buildingId);
    if (matched) return matched;
  }
  const name = lower(hostname);
  if (name) {
    for (const [pattern, hints] of Object.entries(keywordMap)) {
      if (!new RegExp(pattern, 'i').test(name)) continue;
      for (const hint of hints) {
        const matched = findConfigBuilding(config, hint);
        if (matched) return matched;
      }
    }
    // 直接按楼栋名称/ID 关键字匹配（如 JX、教学楼）
    for (const building of config.buildings) {
      const id = lower(building.id);
      const buildingName = lower(building.name);
      if ((id && name.includes(id)) || (buildingName && name.includes(buildingName))) return building;
    }
  }
  return null;
}

export const SCENE_BUILDING_KEYWORDS = {
  '^(SW|PC|AP)-JX': ['teach', '教学', 'JX'],
  '^(SW|PC|AP)-SX': ['train', '实训', 'SX'],
  '^(SW|PC|AP)-SS': ['dorm', '宿舍', 'SS'],
  '^(Core|Router|Server|SRV|NC)': ['core', '机房', 'CORE'],
};
