#!/usr/bin/env node
/**
 * 前端场景 ↔ 后端设备一致性自测。
 *
 * 目的：不只靠肉眼，验证“页面里绘制的设备名 = 后端 /api/devices 返回的设备名”：
 *   1. 拉取后端 /api/devices、/api/hosts、/api/topology、/api/issues、/api/health、/api/network-health；
 *   2. 复用浏览器端同一套 normalize + buildSceneItems 逻辑（src/api/normalize.js、src/scene/items.js）；
 *   3. 双向比对设备名 / 主机名（后端有而场景没有 = 漏渲染；场景有而后端没有 = 疑似硬编码）；
 *   4. 校验楼栋归属、标记坐标是否在对应楼栋范围内；
 *   5. 输出 UTF-8 报告到 scripts/verify-report.txt，异常退出码非 0。
 *
 * 用法：
 *   node scripts/verify-devices.mjs
 *   node scripts/verify-devices.mjs http://127.0.0.1:8000
 *   VITE_API_BASE=http://127.0.0.1:8000 node scripts/verify-devices.mjs
 */
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { normalizeSnapshot } from '../src/api/normalize.js';
import { buildSceneItems, summarizeSceneItems, MARKER_TYPE_LABELS } from '../src/scene/items.js';
import { BUILDINGS, getBuilding } from '../src/scene/layout.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const apiBase = (
  process.argv[2] ||
  process.env.VITE_API_BASE ||
  'http://127.0.0.1:8000'
).replace(/\/+$/, '');

/** 任务书约定的楼栋归属（仅校验后端确实存在的设备；不存在的名字自动跳过） */
const EXPECTED_BUILDING = {
  'Core-3650': '中心机房',
  'Router-4331': '中心机房',
  'SW-JX-2960': '教学楼',
  'SW-SX-2960': '实训楼',
  'SW-SS-2960': '宿舍楼',
  'PC-JX1': '教学楼',
  'PC-JX2': '教学楼',
  'PC-SX1': '实训楼',
  'PC-SS1': '宿舍楼',
  'PC-SS2': '宿舍楼',
  'Server-1': '中心机房',
};

const reportLines = [];
const failures = [];

function out(line = '') {
  reportLines.push(line);
  console.log(line);
}

function check(condition, okMessage, failMessage) {
  if (condition) {
    out(`  [PASS] ${okMessage}`);
  } else {
    out(`  [FAIL] ${failMessage}`);
    failures.push(failMessage);
  }
}

async function getJson(path) {
  const url = `${apiBase}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}（${url}）`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

function formatPosition(position) {
  return `(${position.x.toFixed(1)}, ${position.y.toFixed(1)}, ${position.z.toFixed(1)})`;
}

function pad(text, width) {
  const value = String(text ?? '');
  // 中文按 2 个宽度计算，保证表格对齐
  const visualWidth = [...value].reduce(
    (sum, ch) => sum + (ch.charCodeAt(0) > 255 ? 2 : 1),
    0,
  );
  return value + ' '.repeat(Math.max(0, width - visualWidth));
}

function printTable(items) {
  const header = `${pad('名称', 16)}${pad('类型', 12)}${pad('状态', 10)}${pad('楼栋', 10)}${pad(
    '告警',
    6,
  )}坐标`;
  out(header);
  out('─'.repeat(78));
  for (const item of items) {
    const status = item.statusMeta?.label ?? '无数据';
    const name = item.hostname;
    out(
      `${pad(name, 16)}${pad(MARKER_TYPE_LABELS[item.type] || item.type, 12)}${pad(
        status,
        10,
      )}${pad(item.buildingName, 10)}${pad(item.alert ? '是' : '否', 6)}${formatPosition(
        item.position,
      )}`,
    );
  }
}

async function main() {
  out('校园数字孪生前端 · 设备一致性自测');
  out(`后端地址：${apiBase}`);
  out(`执行时间：${new Date().toISOString()}`);
  out('');

  // ---- 1. 拉取后端数据（设备接口是硬依赖） ----
  const [devicesDoc, hostsDoc, topologyDoc, issuesDoc, healthDoc, healthOverviewDoc] =
    await Promise.all([
      getJson('/api/devices'),
      getJson('/api/hosts'),
      getJson('/api/topology'),
      getJson('/api/issues'),
      getJson('/api/health'),
      getJson('/api/network-health'),
    ]);

  out('后端返回概况：');
  out(`  /api/devices          count=${devicesDoc.count ?? (devicesDoc.devices || []).length}`);
  out(`  /api/hosts            count=${hostsDoc.count ?? (hostsDoc.hosts || []).length}`);
  out(`  /api/topology         nodes=${topologyDoc.counts?.nodes ?? 0} links=${topologyDoc.counts?.links ?? 0}`);
  out(`  /api/issues           count=${issuesDoc.count ?? (issuesDoc.issues || []).length}`);
  out(`  /api/health           driver=${healthDoc.driver} stale=${healthDoc.stale} NC可达=${healthDoc.driver_status?.reachable}`);
  out(
    `  /api/network-health   healthy_devices_percentage=${healthOverviewDoc.network_health?.healthy_devices_percentage ?? '—'}%`,
  );
  out('');

  // ---- 2. 用与浏览器完全相同的逻辑构建场景条目 ----
  const snapshot = normalizeSnapshot({
    devices: devicesDoc.devices || [],
    hosts: hostsDoc.hosts || [],
    issues: issuesDoc.issues || [],
    topology: topologyDoc,
    networkHealth: healthOverviewDoc.network_health ?? null,
    healthStatus: healthDoc,
    alerts: [],
    capturedAt: devicesDoc.captured_at ?? null,
    capturedAtIso: devicesDoc.captured_at_iso ?? null,
    source: devicesDoc.source ?? null,
    stale: Boolean(devicesDoc.stale),
  });
  const items = buildSceneItems(snapshot, []);
  const summary = summarizeSceneItems(items);
  const sceneStats = { buildings: BUILDINGS.length, ...summary };

  out(`场景条目统计：${JSON.stringify(sceneStats)}`);
  out('');

  // ---- 3. 设备名双向一致性 ----
  out('设备名一致性（后端 /api/devices ↔ 场景设备标记）：');
  const backendDeviceNames = (devicesDoc.devices || []).map((device) => device.hostname).sort();
  const sceneDeviceNames = items
    .filter((item) => item.kind === 'device')
    .map((item) => item.hostname)
    .sort();
  out(`  后端设备：${backendDeviceNames.join(', ') || '（空）'}`);
  out(`  场景设备：${sceneDeviceNames.join(', ') || '（空）'}`);

  const missingDevices = backendDeviceNames.filter((name) => !sceneDeviceNames.includes(name));
  const extraDevices = sceneDeviceNames.filter((name) => !backendDeviceNames.includes(name));
  check(
    missingDevices.length === 0,
    `后端 ${backendDeviceNames.length} 台设备全部出现在场景中`,
    `场景缺少后端设备：${missingDevices.join(', ')}`,
  );
  check(
    extraDevices.length === 0,
    '场景没有后端不存在的设备（无硬编码假设备）',
    `场景出现后端没有的设备（疑似硬编码）：${extraDevices.join(', ')}`,
  );
  check(
    backendDeviceNames.length === sceneDeviceNames.length,
    `设备数量一致：${backendDeviceNames.length}`,
    `设备数量不一致：后端 ${backendDeviceNames.length} / 场景 ${sceneDeviceNames.length}`,
  );

  // ---- 4. 主机 / 终端一致性 ----
  out('');
  out('主机 / 终端一致性（后端 /api/hosts ↔ 场景主机标记）：');
  const backendHostNames = (hostsDoc.hosts || []).map((host) => host.hostname).sort();
  const sceneHostNames = items
    .filter((item) => item.kind === 'host')
    .map((item) => item.hostname)
    .sort();
  out(`  后端主机：${backendHostNames.join(', ') || '（空）'}`);
  out(`  场景主机：${sceneHostNames.join(', ') || '（空）'}`);
  const missingHosts = backendHostNames.filter((name) => !sceneHostNames.includes(name));
  const extraHosts = sceneHostNames.filter((name) => !backendHostNames.includes(name));
  check(
    missingHosts.length === 0,
    `后端 ${backendHostNames.length} 台主机全部出现在场景中`,
    `场景缺少后端主机：${missingHosts.join(', ')}`,
  );
  check(
    extraHosts.length === 0,
    '场景没有后端不存在的主机（无硬编码假主机）',
    `场景出现后端没有的主机：${extraHosts.join(', ')}`,
  );

  // ---- 5. 楼栋 / 坐标校验 ----
  out('');
  out('楼栋归属与落位校验：');
  check(BUILDINGS.length === 4, '场景固定渲染 4 栋楼', `楼栋数量异常：${BUILDINGS.length}`);
  let buildingError = 0;
  let positionError = 0;
  for (const item of items) {
    const building = getBuilding(item.buildingId);
    if (!building) {
      buildingError += 1;
      out(`  [FAIL] ${item.hostname} 未分配楼栋`);
      failures.push(`${item.hostname} 未分配楼栋`);
      continue;
    }
    if (!Number.isFinite(item.position.x) || !Number.isFinite(item.position.y) || !Number.isFinite(item.position.z)) {
      positionError += 1;
      out(`  [FAIL] ${item.hostname} 坐标非法：${JSON.stringify(item.position)}`);
      failures.push(`${item.hostname} 坐标非法`);
      continue;
    }
    if (Math.abs(item.position.x - building.x) > building.width / 2) {
      positionError += 1;
      out(`  [FAIL] ${item.hostname} 超出楼栋 ${building.name} 的 x 范围`);
      failures.push(`${item.hostname} 落位超出楼栋范围`);
    }
    if (item.position.y <= building.height) {
      positionError += 1;
      out(`  [FAIL] ${item.hostname} 标记未位于楼顶上方`);
      failures.push(`${item.hostname} 标记未位于楼顶上方`);
    }
  }
  check(buildingError === 0, '所有条目都分配到了楼栋', '存在未分配楼栋的条目');
  check(positionError === 0, '所有标记坐标有效且落在对应楼栋范围内', '存在非法/越界的标记坐标');

  // 任务书约定的楼栋归属（只校验后端实际存在的设备，避免因 PT 环境差异误报）
  let expectedMismatch = 0;
  for (const [hostname, expectedBuilding] of Object.entries(EXPECTED_BUILDING)) {
    const item = items.find((candidate) => candidate.hostname === hostname);
    if (!item) continue; // 后端当前没有这台设备 → 跳过（不虚构）
    if (item.buildingName !== expectedBuilding) {
      expectedMismatch += 1;
      out(`  [FAIL] ${hostname} 期望在「${expectedBuilding}」，实际在「${item.buildingName}」`);
      failures.push(`${hostname} 楼栋归属错误`);
    } else {
      out(`  [PASS] ${hostname} → ${item.buildingName}`);
    }
  }
  check(expectedMismatch === 0, '任务书约定的设备→楼栋归属全部正确', '存在设备→楼栋归属错误');

  // ---- 6. NC 数据源控制器（来自 /api/health，不是 /api/devices 里的设备） ----
  out('');
  const ncReachable = healthDoc.driver_status?.reachable === true;
  const controllerItem = items.find((item) => item.kind === 'controller');
  if (ncReachable) {
    check(
      Boolean(controllerItem),
      `NC 可达 → 场景包含“NC 数据源控制器”标记（驱动=${healthDoc.driver}）`,
      'NC 可达但场景缺少 NC 数据源控制器标记',
    );
  } else {
    out('  [INFO] 后端报告 NC 不可达，场景不显示 NC 控制器标记（符合“只展示后端数据”约束）');
  }

  // ---- 7. 设备明细表 ----
  out('');
  out('场景设备明细（名称 / 类型 / 状态 / 楼栋 / 是否告警 / 坐标）：');
  printTable(items);

  // ---- 8. 汇总 ----
  out('');
  out('='.repeat(78));
  if (failures.length === 0) {
    out(`结果：PASS —— 设备(${sceneDeviceNames.length})/主机(${sceneHostNames.length}) 与后端完全一致，场景 ${items.length} 个标记。`);
  } else {
    out(`结果：FAIL —— 共 ${failures.length} 项问题：`);
    failures.forEach((message, index) => out(`  ${index + 1}. ${message}`));
  }
  out('='.repeat(78));

  // 报告落盘为 UTF-8，便于在控制台编码异常时也能核对
  const reportPath = join(__dirname, 'verify-report.txt');
  writeFileSync(reportPath, reportLines.join('\n') + '\n', 'utf8');
  out(`报告已写入：${reportPath}`);

  process.exitCode = failures.length === 0 ? 0 : 1;
}

main().catch((error) => {
  const message = `自测失败：${error?.message || error}`;
  out(message);
  if (error?.cause) out(`原因：${error.cause}`);
  const reportPath = join(__dirname, 'verify-report.txt');
  try {
    writeFileSync(reportPath, reportLines.join('\n') + '\n', 'utf8');
  } catch {
    /* 报告写入失败不影响退出码 */
  }
  process.exitCode = 1;
});
