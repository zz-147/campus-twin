/**
 * 校园楼栋布局与设备落位（纯 JS，无 three.js 依赖，供浏览器与 Node 校验脚本共用）。
 *
 * 重要边界：本文件**只定义空间布局**（楼栋在哪儿、设备摆放的相对位置），
 * 不定义“有哪些设备”。设备 / 主机 / 告警全部来自后端 API，
 * 因此页面不会出现后端没有的虚构设备。
 */

/** 4 栋楼的位置与体块尺寸（x,z 为地面中心坐标，单位≈米） */
export const BUILDINGS = [
  { id: 'core', name: '中心机房', x: 0, z: 0, width: 48, depth: 36, height: 16, color: 0x2f6f9e },
  { id: 'teach', name: '教学楼', x: -120, z: 40, width: 78, depth: 30, height: 24, color: 0x2a8a7e },
  { id: 'train', name: '实训楼', x: 120, z: 40, width: 62, depth: 30, height: 20, color: 0x7a63b6 },
  { id: 'dorm', name: '宿舍楼', x: 0, z: -140, width: 66, depth: 26, height: 21, color: 0x9a783f },
];

/** 相机默认看向的校园中心（略偏北，让 4 栋楼在画面中更均衡） */
export const CAMPUS_CENTER = { x: 0, z: -42 };

const BUILDING_BY_ID = new Map(BUILDINGS.map((building) => [building.id, building]));

export function getBuilding(buildingId) {
  return BUILDING_BY_ID.get(buildingId) || BUILDINGS[0];
}

export function getBuildingName(buildingId) {
  return getBuilding(buildingId).name;
}

/** 未匹配到楼栋规则时的兜底楼栋（会标记 fallbackBuilding，详情面板里提示“自动归入”） */
export const FALLBACK_BUILDING_ID = 'core';

/**
 * 设备名 → 楼栋规则（按顺序匹配，先命中者生效）。
 * 规则只决定“画在哪栋楼”，不决定设备是否存在。
 */
export const DEVICE_BUILDING_RULES = [
  { pattern: /^(SW|PC|AP)-JX/i, buildingId: 'teach' }, // 教学楼
  { pattern: /^(SW|PC|AP)-SX/i, buildingId: 'train' }, // 实训楼
  { pattern: /^(SW|PC|AP)-SS/i, buildingId: 'dorm' }, // 宿舍楼
  { pattern: /^(Core|Router|Server|SRV|NC)/i, buildingId: 'core' }, // 中心机房
];

/** 按主机名猜测所属楼栋；猜不到返回 null */
export function guessBuildingId(hostname) {
  const name = String(hostname ?? '').trim();
  if (!name) return null;
  for (const rule of DEVICE_BUILDING_RULES) {
    if (rule.pattern.test(name)) return rule.buildingId;
  }
  return null;
}

/** 每行最多摆几个设备标记（楼顶一行） */
export const MAX_ITEMS_PER_ROW = 4;

/**
 * 计算某栋楼第 index 个（共 count 个）设备标记的世界坐标。
 * 设备标记摆在楼顶上方，按行居中排列，避免互相遮挡。
 */
export function computeSlot(building, index, count) {
  const total = Math.max(count, 1);
  const perRow = Math.min(total, MAX_ITEMS_PER_ROW);
  const rows = Math.ceil(total / perRow);
  const row = Math.floor(index / perRow);
  const col = index % perRow;
  const itemsInRow = Math.min(perRow, total - row * perRow);
  const usableWidth = building.width - 12;
  const spacing = itemsInRow > 1 ? Math.min(15, usableWidth / (itemsInRow - 1)) : 0;
  const x = building.x + (col - (itemsInRow - 1) / 2) * spacing;
  const z = building.z + (row - (rows - 1) / 2) * 12;
  return { x, y: building.height + 3.8, z };
}

/** 落位排序：网络设备 → 主机/终端 → 数据源控制器；同类按主机名，保证每次刷新位置稳定 */
export function sortItemsForLayout(a, b) {
  const priority = { device: 0, host: 1, controller: 2 };
  const pa = priority[a.kind] ?? 3;
  const pb = priority[b.kind] ?? 3;
  if (pa !== pb) return pa - pb;
  const na = String(a.hostname ?? '');
  const nb = String(b.hostname ?? '');
  if (na < nb) return -1;
  if (na > nb) return 1;
  return 0;
}
