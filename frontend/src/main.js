/**
 * 入口装配层。
 *
 * 数据流：
 *   后端 REST /api/* + WebSocket /ws
 *     → api/client.js（连接、重连、快照聚合）
 *     → api/normalize.js（字段归一化 / 状态分类 / W3 时间基准）
 *     → scene/items.js（楼栋归属 + 落位 + 活跃告警匹配）
 *     → scene/CampusScene.js（3D 渲染 + 点击聚焦）
 *     → ui/hud.js（顶部状态栏 / 告警列表 / 右侧信息坞）
 *
 * AI 通道（独立于大屏数据）：
 *   ui/aiPanel.js → api/agentClient.js → agent 服务 :8100（真实 DeepSeek）
 *   agent 不可用时只在 AI 页签提示“未连接”，不影响 3D 大屏与告警展示。
 *
 * 真实性约束：页面上的设备 / 主机 / 告警全部来自后端响应；
 * 后端不可用时显示“未连接”，只有用户手动打开“演示数据”开关才加载内置 mock，
 * 且界面会一直显示“演示数据 · 非真实采集”水印。
 */
import './styles.css';
import { API_BASE, DataClient } from './api/client.js';
import { AGENT_BASE, AgentClient } from './api/agentClient.js';
import { AdminClient } from './api/adminClient.js';
import { buildAlertEntries, countActiveEntries } from './api/alertsView.js';
import { normalizeSnapshot } from './api/normalize.js';
import { demoBundle } from './api/mock.js';
import { buildSceneItems, summarizeSceneItems } from './scene/items.js';
import { fallbackSceneConfig } from './scene/sceneConfig.js';
import { CampusScene } from './scene/CampusScene.js';
import { Hud } from './ui/hud.js';
import { AiPanel } from './ui/aiPanel.js';
import { NewDevicesBanner } from './ui/newDevices.js';
import { PathPanel } from './ui/pathPanel.js';
import { ObservabilityPanel } from './ui/observability.js';
import { confirmDialog } from './ui/dialog.js';

/** 页面运行态（不含任何设备数据；设备数据只在 snapshot 里） */
const state = {
  backendOnline: false,
  mockEnabled: false,
  demo: false,
  snapshot: null,
  items: [],
  itemsByKey: new Map(),
  liveAlerts: [],
  selectedKey: null,
  // W10：告警视图 / 根因分组 / 路径追踪 / 通知状态
  alertView: 'all',
  alertGroups: null,
  alertGroupsError: '',
  pathTrace: null,
  notifyStatus: null,
};

let scene = null;
let hud = null;
let client = null;
let aiPanel = null;
let agent = null;
let admin = null;
let newDeviceBanner = null;
let pathPanel = null;
let obsPanel = null;

// --------------------------------------------------------------------------- //
// 启动
// --------------------------------------------------------------------------- //
function boot() {
  hud = new Hud(document, {
    onToggleRotate: (checked) => scene?.setAutoRotate(checked),
    onToggleMock: (checked) => setMockEnabled(checked),
    onRefresh: () => {
      hud.toast('正在刷新数据…');
      client?.refresh();
    },
    onResetView: () => scene?.resetView(),
    onDeselect: () => scene?.clearSelection(),
    onAlertDevice: (deviceName) => focusDeviceByName(deviceName),
    // W9：告警数据管理（删除/静默）+ 清空已恢复
    onAlertAction: (payload) => handleAlertAction(payload),
    onClearCleared: () => handleClearCleared(),
    // W10：告警视图切换 / 通知测试
    onAlertViewChange: (view) => setAlertView(view),
    onNotifyTest: () => handleNotifyTest(),
  });

  admin = new AdminClient();
  pathPanel = new PathPanel(document, {
    onTrace: (sourceIp, destIp) => handlePathTrace(sourceIp, destIp),
    onClear: () => handlePathClear(),
  });
  newDeviceBanner = new NewDevicesBanner(document, {
    onConfirm: (payload) => handleNewDeviceConfirm(payload),
    onRemove: (payload) => handleNewDeviceRemove(payload),
    onViewConfig: (payload) => handleNewDeviceConfig(payload),
  });

  hud.setLoading(true, '正在初始化数字孪生场景…', '正在构建 4 栋楼宇与设备标记');
  window.addEventListener('error', (event) => {
    hud?.setError(`前端脚本运行错误：${event.message || event.error}`);
  });
  window.addEventListener('unhandledrejection', (event) => {
    hud?.setError(`未处理的异步错误：${event.reason?.message || event.reason}`);
  });

  // AI 运维助手（独立服务 :8100，不阻塞 3D 大屏初始化）
  agent = new AgentClient();
  aiPanel = new AiPanel(document, {
    agent,
    onStatus: (status) => hud?.setAiStatus(status),
    onToast: (message, type) => hud?.toast(message, type),
    // W9：Agent action 执行成功后刷新告警/场景数据
    onDataChanged: () => {
      hud?.toast('数据已刷新');
      client?.refresh();
    },
  });

  // W15-P2：可观测性页签（懒加载，不阻塞主流程；接口未就绪时仅该页签降级）
  obsPanel = new ObservabilityPanel(document, {
    onLoad: (options) => loadObservabilityData(options),
    onLoadTrace: (traceId) => agent.trace(traceId),
    onChange: () => updateDebugStats(),
  });
  document.getElementById('tab-obs')?.addEventListener('click', () => {
    obsPanel.setCurrentConversation(aiPanel?.conversationId || null);
    obsPanel.load();
  });

  try {
    scene = new CampusScene(document.getElementById('scene-root'), {
      onSelect: (item) => {
        state.selectedKey = item.key;
        hud.hideTooltip(); // 选中后由右侧详情面板展示，悬浮提示收起
        hud.setSelectedItem(item, { focusTab: true });
        updateDebugStats();
      },
      onDeselect: () => {
        state.selectedKey = null;
        hud.setSelectedItem(null);
        updateDebugStats();
      },
      onHover: (item, event) => {
        if (item && event) hud.showTooltip(item, event);
        else hud.hideTooltip();
      },
    });
  } catch (error) {
    hud.setError(`3D 场景初始化失败（浏览器可能未启用 WebGL）：${error?.message || error}`);
    hud.setLoading(false);
    return;
  }

  scene.setAutoRotate(hud.isAutoRotateEnabled());
  hud.setLoading(true, '正在连接后端…', `API：${API_BASE}`);
  client = new DataClient({
    onSnapshot: handleSnapshot,
    onAlert: handleLiveAlert,
    onAlertsCleared: handleAlertsCleared,
    onConnection: handleConnection,
    onStatus: handleStatus,
  });
  client.start();

  // 楼栋在场景初始化时已经渲染；无论后端是否就绪，1.2s 后都收起加载遮罩
  setTimeout(() => hud.setLoading(false), 1200);

  // 浏览器控制台调试入口（只读 + 手动刷新，不影响页面逻辑）
  window.__campusTwin = {
    state,
    apiBase: API_BASE,
    agentBase: AGENT_BASE,
    refresh: () => client.refresh(),
    get scene() {
      return scene;
    },
    get ai() {
      return aiPanel;
    },
    get agent() {
      return agent;
    },
    get admin() {
      return admin;
    },
    get newDeviceBanner() {
      return newDeviceBanner;
    },
    get pathPanel() {
      return pathPanel;
    },
    get obsPanel() {
      return obsPanel;
    },
  };

  updateDebugStats();
}

// --------------------------------------------------------------------------- //
// 数据回调
// --------------------------------------------------------------------------- //
function handleSnapshot(snapshot) {
  const wasDemo = state.demo;
  state.backendOnline = true;
  state.demo = Boolean(snapshot.demo);
  state.snapshot = snapshot;
  applySnapshot(snapshot);
  if (wasDemo && !state.demo) {
    hud.toast('后端已连接，已自动切回真实采集数据');
  }
}

function handleConnection({ online, changed, wsOpen, apiBase, error }) {
  const wasOnline = state.backendOnline;
  state.backendOnline = online;
  hud.setConnection({ online, wsOpen, apiBase, error });
  if (online) {
    hud.setError('');
    return;
  }
  if (!changed) return;
  if (state.mockEnabled) {
    activateDemo();
  } else if (!state.demo) {
    clearSceneData();
    if (wasOnline) hud.toast('后端连接中断，真实数据不可用', 'error');
  }
}

function handleLiveAlert(alert) {
  if (!alert) return;
  // W3：info 级是设备上线/新增链路的“一次性信息事件”，不进告警列表
  if (alert.state === 'info' || alert.severity === 'info') {
    hud.toast(`实时事件：${alert.title}`);
    return;
  }
  state.liveAlerts = [alert, ...state.liveAlerts.filter((item) => item.key !== alert.key)].slice(0, 30);
  if (state.snapshot && !state.demo) {
    refreshSceneFromSnapshot();
    renderAlertPanel();
  }
  hud.toast(`实时告警：${alert.title}`, alert.severity === 'critical' ? 'error' : 'warn');
}

/** W3：恢复事件被 pipeline 聚合为一条摘要（不再逐条刷屏） */
function handleAlertsCleared(summary) {
  if (!summary) return;
  const clearedKeys = new Set(
    (summary.items || []).map((item) => `alert:${item.dedup_key || item.dedupKey || ''}`),
  );
  state.liveAlerts = state.liveAlerts.filter((alert) => !clearedKeys.has(alert.key));
  if (state.snapshot && !state.demo) {
    refreshSceneFromSnapshot();
    renderAlertPanel();
  }
  hud.toast(summary.summary || `本轮清除 ${summary.count || 0} 条告警`, 'warn');
}

function handleStatus(status) {
  if (!status || !state.snapshot || state.demo) return;
  // 采集状态变化（如 NC 掉线变 stale）：只更新状态栏，不重建场景
  state.snapshot.stale = Boolean(status.stale);
  state.snapshot.collect.driver = status.driver || state.snapshot.collect.driver;
  if (status.detection) state.snapshot.detection = state.snapshot.detection || status.detection;
  if (status.hosts_observed_at) {
    state.snapshot.freshness.observedAtIso = status.hosts_observed_at;
  }
  hud.updateStats(state.snapshot, { demo: false });
  updateDebugStats();
}

// --------------------------------------------------------------------------- //
// 场景 / UI 数据应用
// --------------------------------------------------------------------------- //
function applySnapshot(snapshot) {
  // W9：先把配置驱动的楼栋应用到 3D 场景（配置变化时动态重建），再应用设备条目
  if (snapshot.sceneConfig?.buildings?.length) {
    scene.setBuildings(snapshot.sceneConfig.buildings);
  }
  state.items = buildSceneItems(snapshot, state.liveAlerts);
  state.itemsByKey = new Map(state.items.map((item) => [item.key, item]));
  scene.applyItems(state.items, snapshot.topology?.links || []);
  hud.updateStats(snapshot, { demo: state.demo });
  hud.setSceneMeta(snapshot.sceneMeta || {});
  hud.setNotifyStatus(snapshot.notifyStatus);
  state.notifyStatus = snapshot.notifyStatus || null;
  renderAlertPanel();
  hud.setSelectedItem(state.selectedKey ? state.itemsByKey.get(state.selectedKey) : null);
  hud.setDemo(state.demo);
  hud.setLoading(false);
  // W9：新设备（pending）提示条
  newDeviceBanner?.update(snapshot.pendingDevices || [], snapshot.sceneConfig?.buildings || []);
  // W10：路径追踪端点下拉 + 数据刷新后自动重绘路径（不重复拉镜头）
  pathPanel?.setEndpoints(state.items, snapshot.pendingDevices || []);
  if (state.pathTrace) scene.setPathTrace(state.pathTrace, { focus: false });
  // W10：分组视图下数据刷新后重新拉取根因分组
  if (state.alertView === 'groups') loadAlertGroups();
  updateDebugStats();
}

function refreshSceneFromSnapshot() {
  if (!state.snapshot) return;
  state.items = buildSceneItems(state.snapshot, state.liveAlerts);
  state.itemsByKey = new Map(state.items.map((item) => [item.key, item]));
  scene.applyItems(state.items, state.snapshot.topology?.links || []);
  if (state.selectedKey) {
    hud.setSelectedItem(state.itemsByKey.get(state.selectedKey) || null);
  }
  newDeviceBanner?.update(
    state.snapshot.pendingDevices || [],
    state.snapshot.sceneConfig?.buildings || [],
  );
  pathPanel?.setEndpoints(state.items, state.snapshot.pendingDevices || []);
  if (state.pathTrace) scene.setPathTrace(state.pathTrace, { focus: false });
  updateDebugStats();
}

/** 统一渲染告警列表（W3 生命周期统计 + 检测引擎 + W7.1 stale_source 语义 + W10 根因分组） */
function renderAlertPanel() {
  if (!state.snapshot) {
    hud.renderAlerts([], { active: 0, cleared: 0, detection: null });
    return;
  }
  const entries = buildAlertEntries(state.snapshot, state.liveAlerts, { resolveDeviceName });
  const summary = state.snapshot.alertsSummary || {};
  // 活跃数只认 active alerts（stale_source 的 issue 不计入）
  const activeFromEntries = countActiveEntries(entries);
  const meta = {
    active: Math.max(Number(summary.active) || 0, activeFromEntries),
    cleared: Number(summary.cleared) || 0,
    total: summary.total,
    detection: state.snapshot.detection,
  };
  hud.setAlertView(state.alertView);
  if (state.alertView === 'groups') {
    if (state.alertGroups) {
      hud.renderAlertGroups(state.alertGroups, entries, meta);
      hud.setGroupsHint(
        `根因分组 ${state.alertGroups.length} 组` +
          (state.alertGroups.some((group) => group.suppressed_count)
            ? ' · 被抑制成员灰显展示'
            : ''),
      );
    } else if (state.alertGroupsError) {
      // 分组接口未就绪/失败：降级显示普通告警列表，避免面板空白
      hud.setGroupsHint(state.alertGroupsError, 'warn');
      hud.renderAlerts(entries, meta);
    } else {
      hud.setGroupsHint('正在加载根因分组…');
      hud.renderAlerts(entries, meta);
    }
    return;
  }
  hud.setGroupsHint('');
  hud.renderAlerts(entries, meta);
}

/** W10：切换「全部 / 按根因分组」视图 */
function setAlertView(view) {
  state.alertView = view === 'groups' ? 'groups' : 'all';
  hud.setAlertView(state.alertView);
  if (state.alertView === 'groups') {
    state.alertGroups = null;
    state.alertGroupsError = '';
    hud.setGroupsHint('正在加载根因分组…');
    renderAlertPanel();
    loadAlertGroups();
  } else {
    hud.setGroupsHint('');
    renderAlertPanel();
  }
  updateDebugStats();
}

/** W10：拉取告警根因分组（GET /api/alerts/groups?state=active） */
async function loadAlertGroups() {
  if (!admin || state.demo) return;
  try {
    const data = await admin.alertGroups('active');
    state.alertGroups = Array.isArray(data?.groups) ? data.groups : [];
    state.alertGroupsError = '';
    if (state.alertView === 'groups') renderAlertPanel();
  } catch (error) {
    state.alertGroups = null;
    state.alertGroupsError =
      error?.kind === 'unsupported'
        ? '根因分组服务未就绪（后端 /api/alerts/groups 未上线）'
        : `根因分组加载失败：${error?.message || error}`;
    if (state.alertView === 'groups') renderAlertPanel();
  } finally {
    updateDebugStats();
  }
}

function clearSceneData() {
  state.snapshot = null;
  state.items = [];
  state.itemsByKey = new Map();
  state.selectedKey = null;
  state.demo = false;
  state.alertGroups = null;
  state.alertGroupsError = '';
  state.pathTrace = null;
  // 回到默认 4 栋楼布局（后端离线时不残留上次配置的场景）
  scene.setBuildings(fallbackSceneConfig().buildings);
  scene.applyItems([], []);
  scene.clearPath();
  pathPanel?.clearView();
  pathPanel?.setEndpoints([], []);
  hud.updateStats(null, { demo: false });
  hud.setSceneMeta({ source: 'fallback', version: 0, counts: { buildings: 4, devices: 0 } });
  hud.renderAlerts([], { active: 0, cleared: 0, detection: null });
  hud.setNotifyStatus(null);
  hud.setSelectedItem(null);
  hud.setDemo(false);
  hud.setGroupsHint('');
  newDeviceBanner?.update([], []);
  obsPanel?.reset();
  updateDebugStats();
}

// --------------------------------------------------------------------------- //
// 演示数据开关（默认关闭；仅后端离线时生效）
// --------------------------------------------------------------------------- //
function setMockEnabled(enabled) {
  state.mockEnabled = Boolean(enabled);
  if (state.mockEnabled && !state.backendOnline) {
    activateDemo();
  } else if (!state.mockEnabled && state.demo) {
    clearSceneData();
    hud.toast('已关闭演示数据');
  } else if (state.mockEnabled && state.backendOnline) {
    hud.toast('后端在线，当前展示真实采集数据；离线时将自动启用演示数据');
  }
}

function activateDemo() {
  const snapshot = normalizeSnapshot(demoBundle());
  state.snapshot = snapshot;
  state.demo = true;
  applySnapshot(snapshot);
  hud.toast('已切换到内置演示数据（非真实采集）', 'warn');
}

// --------------------------------------------------------------------------- //
// 告警列表 / 定位
// --------------------------------------------------------------------------- //
/** 告警里的 device_id 可能是设备/主机 UUID，用当前场景条目映射回主机名 */
function resolveDeviceName(deviceId) {
  const target = String(deviceId || '').trim().toLowerCase();
  if (!target) return '';
  const item = state.items.find(
    (candidate) =>
      String(candidate.id).toLowerCase() === target ||
      String(candidate.hostname).toLowerCase() === target,
  );
  return item ? item.hostname : deviceId;
}

function focusDeviceByName(deviceName) {
  const target = String(deviceName || '').trim().toLowerCase();
  if (!target) return;
  const item = state.items.find((candidate) => String(candidate.hostname).toLowerCase() === target);
  if (item) {
    scene.selectItem(item.key);
  } else {
    hud.toast(`设备「${deviceName}」不在当前设备快照中`, 'warn');
  }
}

// --------------------------------------------------------------------------- //
// W9：告警数据管理（删除 / 清空已恢复 / 静默，不依赖 Agent）
// --------------------------------------------------------------------------- //
function handleAdminError(error, actionLabel) {
  if (error?.kind === 'unsupported') {
    hud.toast(`后端 W9 接口尚未就绪：${actionLabel}失败（404）`, 'warn');
  } else {
    hud.toast(`${actionLabel}失败：${error?.message || error}`, 'error');
  }
}

async function handleAlertAction({ action, alertId } = {}) {
  if (!alertId) return;
  if (action === 'silence') {
    const confirmed = await confirmDialog({
      title: '静默活跃告警',
      message:
        '静默后该活跃告警不再提醒，但记录仍保留、故障不会被删除；如需恢复提醒请等待故障恢复。',
      confirmText: '静默',
    });
    if (!confirmed) return;
    try {
      await admin.silenceAlert(alertId);
      hud.toast('已静默该活跃告警');
      client.refresh();
    } catch (error) {
      handleAdminError(error, '静默告警');
    }
    return;
  }
  if (action !== 'delete') return;
  const confirmed = await confirmDialog({
    title: '删除已恢复告警',
    message: '将软删除该条已恢复告警（写入审计日志，10 秒内可撤销）。',
    confirmText: '删除',
    danger: true,
  });
  if (!confirmed) return;
  try {
    await admin.deleteAlert(alertId);
    hud.toast('已删除该条告警', 'info', {
      actionLabel: '撤销',
      duration: 10000,
      onAction: async () => {
        try {
          await admin.restoreAlert(alertId);
          hud.toast('已撤销删除');
          client.refresh();
        } catch (error) {
          handleAdminError(error, '撤销删除');
        }
      },
    });
    client.refresh();
  } catch (error) {
    handleAdminError(error, '删除告警');
  }
}

async function handleClearCleared() {
  const summary = state.snapshot?.alertsSummary || {};
  const clearedCount = Math.max(0, Number(summary.cleared) || 0);
  const confirmed = await confirmDialog({
    title: '清空已恢复告警',
    message: `将清空 ${clearedCount} 条已恢复告警（软删除并写入审计日志）。活跃告警不会被删除。`,
    confirmText: '清空已恢复',
    danger: true,
  });
  if (!confirmed) return;
  try {
    const result = await admin.clearClearedAlerts();
    hud.toast(`已清空 ${result?.deleted ?? clearedCount} 条已恢复告警`);
    client.refresh();
  } catch (error) {
    handleAdminError(error, '清空已恢复告警');
  }
}

// --------------------------------------------------------------------------- //
// W9：新设备确认 / 撤销（配置驱动场景自动发现）
// --------------------------------------------------------------------------- //
async function handleNewDeviceConfirm({ name, buildingId } = {}) {
  if (!name) return;
  const pending = (state.snapshot?.pendingDevices || []).find((device) => device.name === name);
  const buildings = state.snapshot?.sceneConfig?.buildings || [];
  const targetBuildingId =
    buildingId ||
    pending?.buildingId ||
    buildings[0]?.id ||
    state.items.find((item) => item.hostname === name)?.buildingId;
  if (!targetBuildingId) {
    hud.toast('无法确定该设备所属楼栋，请先在提示条中选择楼栋', 'warn');
    return;
  }
  try {
    // 未推断出楼栋的设备：先写入楼栋位置（upsert），再确认
    if (!pending?.buildingId) {
      await admin.upsertBuildingDevice(targetBuildingId, {
        name,
        hostname: name,
        type: pending?.type || 'unknown',
        status: 'pending',
        ...(pending?.model ? { model: pending.model } : {}),
        ...(pending?.ip ? { ip: pending.ip } : {}),
      });
    }
    await admin.confirmSceneDevice(name);
    hud.toast(`已确认新设备「${name}」加入场景`);
    client.refresh();
  } catch (error) {
    handleAdminError(error, '确认新设备');
  }
}

async function handleNewDeviceRemove({ name } = {}) {
  if (!name) return;
  const confirmed = await confirmDialog({
    title: '撤销新设备',
    message: `将从场景配置中移除「${name}」。该操作会写入审计日志。`,
    confirmText: '撤销加入',
    danger: true,
  });
  if (!confirmed) return;
  try {
    await admin.removeSceneDevice(name);
    hud.toast(`已撤销新设备「${name}」`);
    client.refresh();
  } catch (error) {
    handleAdminError(error, '撤销新设备');
  }
}

function handleNewDeviceConfig({ name } = {}) {
  const pending = (state.snapshot?.pendingDevices || []).find((device) => device.name === name);
  if (!pending) return;
  console.info('[campus-twin] 新设备场景配置：', pending);
  hud.toast(`「${name}」场景配置已输出到浏览器控制台`);
}

// --------------------------------------------------------------------------- //
// W10：路径追踪 / 通知测试
// --------------------------------------------------------------------------- //
async function handlePathTrace(sourceIp, destIp) {
  if (!admin) return;
  pathPanel?.setLoading(true);
  try {
    const trace = await admin.pathTrace(sourceIp, destIp);
    if (!trace || trace.ok === false) {
      const message = trace?.error || '路径追踪失败（后端未返回有效路径）';
      state.pathTrace = null;
      scene.clearPath();
      pathPanel?.renderError(message);
      hud.toast(message, 'error');
      updateDebugStats();
      return;
    }
    state.pathTrace = trace;
    scene.setPathTrace(trace, { focus: true });
    pathPanel?.renderTrace(trace);
    const traceStatus = String(trace.trace_status || trace.status || 'complete').toLowerCase();
    hud.toast(
      traceStatus === 'complete'
        ? `路径追踪完成：${trace.hops?.length || 0} 跳，3D 场景已高亮`
        : '路径仅部分可达，已在 3D 场景标记断点',
      traceStatus === 'complete' ? 'info' : 'warn',
    );
    updateDebugStats();
  } catch (error) {
    state.pathTrace = null;
    scene.clearPath();
    const message =
      error?.kind === 'unsupported'
        ? '路径追踪服务未就绪（后端 /api/path-trace 未上线）'
        : `路径追踪失败：${error?.message || error}`;
    pathPanel?.renderError(message);
    hud.toast(message, 'error');
    updateDebugStats();
  }
}

function handlePathClear() {
  state.pathTrace = null;
  scene.clearPath();
  pathPanel?.clearView();
  updateDebugStats();
}

async function handleNotifyTest() {
  if (!admin) return;
  hud.setNotifyTestBusy(true);
  try {
    const result = await admin.notifyTest();
    const sent = result?.ok !== false && !result?.error;
    const message = sent
      ? result?.message || '测试通知已发送（Webhook 返回成功）'
      : `测试通知发送失败${result?.error ? `：${result.error}` : ''}`;
    hud.toast(message, sent ? 'info' : 'error');
    client?.refresh(); // 刷新通知状态（last_sent_at / sent_count）
  } catch (error) {
    const message =
      error?.kind === 'unsupported'
        ? '通知服务未就绪（后端 /api/notify/test 未上线）'
        : `发送测试通知失败：${error?.message || error}`;
    hud.toast(message, 'error');
  } finally {
    hud.setNotifyTestBusy(false);
  }
}

// --------------------------------------------------------------------------- //
// W15-P2：可观测性数据加载（metrics / traces / RAG；接口 404 时返回错误由面板降级）
// --------------------------------------------------------------------------- //
async function loadObservabilityData({ filterCurrent = false, conversationId = null } = {}) {
  if (!agent) return {};
  const targetConversation = filterCurrent ? conversationId : null;
  const [metricsResult, tracesResult, ragResult] = await Promise.allSettled([
    agent.metricsAgent(),
    targetConversation
      ? agent.traces({ conversationId: targetConversation, limit: 20 })
      : agent.traces({ limit: 20 }),
    agent.ragReport(),
  ]);
  const pick = (result) => (result.status === 'fulfilled' ? result.value : null);
  const pickError = (result) => (result.status === 'rejected' ? result.reason : null);
  const noConversation = filterCurrent && !targetConversation;
  return {
    metrics: pick(metricsResult),
    metricsError: pickError(metricsResult),
    traces: noConversation ? { count: 0, items: [] } : pick(tracesResult),
    tracesError: noConversation ? null : pickError(tracesResult),
    rag: pick(ragResult),
    ragError: pickError(ragResult),
    noConversation,
  };
}

// --------------------------------------------------------------------------- //
// 自测辅助：把场景/数据/AI 统计写进隐藏节点，便于 --dump-dom / 控制台核对
// --------------------------------------------------------------------------- //
function updateDebugStats() {
  const node = document.getElementById('dev-stats');
  if (!node) return;
  const objectStats = scene?.getObjectStats?.() || {};
  const pathStats = scene?.getPathStats?.() || {};
  const itemSummary = summarizeSceneItems(state.items, state.snapshot?.sceneConfig);
  const freshness = state.snapshot?.freshness || {};
  const sceneMeta = state.snapshot?.sceneMeta || {};
  const notify = state.snapshot?.notifyStatus || null;
  const streamDebug = aiPanel?.getStreamDebug?.() || null;
  const obsDebug = obsPanel?.getDebug?.() || null;
  const groupSuppressed = Array.isArray(state.alertGroups)
    ? state.alertGroups.reduce(
        (sum, group) => sum + (Number(group.suppressed_count) || (group.members || []).filter((m) => m.suppressed_by).length),
        0,
      )
    : 0;
  node.textContent = JSON.stringify({
    api: API_BASE,
    agentBase: AGENT_BASE,
    online: state.backendOnline,
    demo: state.demo,
    // W9：配置驱动场景统计（楼栋数不再是写死的 4）
    buildings: itemSummary.buildings,
    sceneSource: sceneMeta.source ?? 'fallback',
    sceneVersion: sceneMeta.version ?? 0,
    scenePendingDevices: (state.snapshot?.pendingDevices || []).length,
    pendingMarkers: objectStats.pendingMarkers ?? 0,
    highlightedMarkers: objectStats.highlightedMarkers ?? 0,
    buildingHighlighting: objectStats.buildingHighlighting ?? false,
    lastNewMarkers: objectStats.lastNewMarkers ?? 0,
    objects: objectStats.objects ?? null,
    nodes: objectStats.nodes ?? null,
    markers: objectStats.markers ?? null,
    links: objectStats.links ?? null,
    alertMarkers: objectStats.alertMarkers ?? null,
    devices: itemSummary.devices,
    hosts: itemSummary.hosts,
    controllers: itemSummary.controllers,
    pending: itemSummary.pending,
    source: state.snapshot?.source ?? null,
    stale: state.snapshot?.stale ?? null,
    capturedAt: state.snapshot?.capturedAtIso ?? null,
    observedAt: freshness.observedAtIso ?? null,
    snapshotAgeSeconds: freshness.snapshotAgeSeconds ?? null,
    alertsActive: state.snapshot?.alertsSummary?.active ?? 0,
    alertsCleared: state.snapshot?.alertsSummary?.cleared ?? 0,
    alertsTotal: state.snapshot?.alertsSummary?.total ?? 0,
    // W7.1：区分“真正的活跃 issue”和“NC 滞后（stale_source）issue”
    issuesTotal: state.snapshot?.issues?.length ?? 0,
    issuesActive: state.snapshot?.summary?.issuesActive ?? 0,
    issuesStale: state.snapshot?.summary?.issuesStale ?? 0,
    detectionEngine: state.snapshot?.detection?.engine ?? null,
    aiOnline: aiPanel?.status?.online ?? false,
    aiProvider: aiPanel?.status?.provider ?? null,
    aiModel: aiPanel?.status?.model ?? null,
    aiBusy: aiPanel?.isBusy?.() ?? false,
    aiConversationId: aiPanel?.conversationId ?? null,
    // W12：流式 SSE 指标（事件顺序 / token 拼接 / 停止 / 回退 / 引用）
    aiStreamEvents: streamDebug?.events?.join(',') ?? '',
    aiStreamStages: (streamDebug?.stages || []).map((stage) => stage.stage).join(','),
    aiStreamTokens: streamDebug?.tokensJoined?.length ?? 0,
    aiStreamChunks: streamDebug?.tokenChunks ?? 0,
    aiStreamStopped: Boolean(streamDebug?.stopped),
    aiStreamFallback: Boolean(streamDebug?.fallback),
    aiCitations: streamDebug?.citations ?? 0,
    // W15：可观测性页签
    obsLoaded: Boolean(obsDebug?.loaded),
    obsMetricsAvailable: Boolean(obsDebug?.metricsAvailable),
    obsTracesCount: obsDebug?.tracesCount ?? 0,
    obsRagAvailable: Boolean(obsDebug?.ragAvailable),
    obsExpanded: obsDebug?.expanded ?? 0,
    obsError: obsDebug?.error ?? '',
    // W10：路径追踪 / 告警根因分组 / 通知状态
    pathActive: Boolean(pathStats.active),
    pathHops: pathStats.hops ?? 0,
    pathBreakpoint: Boolean(pathStats.breakpoint),
    pathBreakpointDevice: pathStats.breakpointDevice ?? null,
    pathObjects: pathStats.objects ?? 0,
    pathLabels: pathStats.labels ?? 0,
    pathSourceIp: state.pathTrace?.source_ip ?? null,
    pathDestIp: state.pathTrace?.dest_ip ?? null,
    pathStatus: state.pathTrace?.trace_status ?? null,
    alertView: state.alertView,
    alertGroupsCount: Array.isArray(state.alertGroups) ? state.alertGroups.length : null,
    alertGroupsSuppressed: groupSuppressed,
    alertGroupsError: state.alertGroupsError || '',
    notifyAvailable: Boolean(notify?.available),
    notifyEnabled: Boolean(notify?.enabled),
    notifyType: notify?.type ?? null,
    notifySentCount: notify?.sentCount ?? null,
    selected: state.selectedKey,
    updatedAt: new Date().toISOString(),
  });
}

// --------------------------------------------------------------------------- //
try {
  boot();
} catch (error) {
  // 极端情况下（容器缺失等）保证页面有可读错误，而不是白屏
  const runtimeError = document.getElementById('runtime-error');
  if (runtimeError) {
    runtimeError.textContent = `初始化失败：${error?.message || error}`;
    runtimeError.classList.remove('hidden');
  }
  const loadingText = document.getElementById('loading-text');
  if (loadingText) loadingText.textContent = `初始化失败：${error?.message || error}`;
  console.error('[campus-twin] 初始化失败', error);
}
