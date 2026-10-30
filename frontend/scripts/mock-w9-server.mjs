/**
 * W9 前端联调用的 Mock 后端 + Mock Agent（仅测试用，不进入生产构建）。
 *
 * 用途：backend / agent 的 W9 接口在并行开发时，前端可以按设计契约先联调与冒烟：
 *   - 后端 mock（默认 8002）：/api/scene、/api/scene/pending、告警删除/清空/静默/撤销、审计
 *   - Agent mock（默认 8102）：/api/chat（返回 actions[]）、/api/actions/{id}/confirm|cancel、
 *     /api/conversations 会话列表/详情/删除/清空
 *
 * 启动：
 *   node scripts/mock-w9-server.mjs
 *   node scripts/mock-w9-server.mjs --backend-port 8002 --agent-port 8102
 *
 * 测试辅助端点（仅 mock 有，用于模拟“PT 里新增设备 → 自动发现”）：
 *   POST /__mock/add_pending?name=AP-NEW-1&building_id=lab
 *   POST /__mock/reset
 */
import { createServer } from 'node:http';

const args = process.argv.slice(2);
function argValue(name, fallback) {
  const index = args.indexOf(name);
  if (index >= 0 && args[index + 1]) return args[index + 1];
  const prefixed = args.find((arg) => arg.startsWith(`${name}=`));
  return prefixed ? prefixed.slice(name.length + 1) : fallback;
}

const BACKEND_PORT = Number(argValue('--backend-port', '8002'));
const AGENT_PORT = Number(argValue('--agent-port', '8102'));
const NOW = () => Date.now() / 1000;

// --------------------------------------------------------------------------- //
// 共享状态（backend 与 agent mock 在同一进程内，动作确认可直接联动数据）
// --------------------------------------------------------------------------- //
function seedState() {
  return {
    sceneVersion: 3,
    // W12：SSE 流式接口模拟模式（ok / 404 / error，由 /__mock/stream_mode 切换）
    streamMode: 'ok',
    // W15：可观测性接口模拟模式（ok / 404 / no_rag，由 /__mock/w15_mode 切换）
    w15Mode: 'ok',
    audit: [],
    alerts: [
      {
        id: 101,
        alert_type: 'device_unreachable',
        severity: 'warning',
        title: '设备采集异常：SW-SX-2960',
        detail: 'reachabilityStatus=Reachable，collectionStatus=Unreachable',
        dedup_key: 'device_unreachable:d-sx',
        device_id: 'd-sx',
        observed_at: NOW() - 600,
        source_ts: '2026-10-30 17:10:00',
        state: 'cleared',
        resolved_at: NOW() - 540,
        clear_reason: '数据源实际状态已恢复',
        stale_source: false,
        source: 'netcon',
      },
      {
        id: 102,
        alert_type: 'host_down',
        severity: 'critical',
        title: '主机离线：PC-JX1',
        detail: 'pingStatus=FAILURE',
        dedup_key: 'host_down:h-pc-jx1',
        device_id: 'h-pc-jx1',
        observed_at: NOW() - 900,
        source_ts: '2026-10-30 17:05:00',
        state: 'cleared',
        resolved_at: NOW() - 840,
        clear_reason: 'pingStatus FAILURE -> SUCCESS',
        stale_source: false,
        source: 'netcon',
      },
      {
        id: 103,
        alert_type: 'device_unreachable',
        severity: 'warning',
        title: '设备采集异常：SW-JX-2960',
        detail: 'collectionStatus=Unreachable（仍在活跃）',
        dedup_key: 'device_unreachable:d-jx',
        device_id: 'd-jx',
        observed_at: NOW() - 120,
        source_ts: '2026-10-30 17:58:00',
        state: 'active',
        resolved_at: null,
        clear_reason: null,
        stale_source: false,
        source: 'netcon',
      },
    ],
    deletedAlerts: [],
    notify: {
      enabled: true,
      type: 'webhook',
      min_severity: 'critical',
      last_sent_at: NOW() - 240,
      last_error: null,
      sent_count: 3,
      url: 'http://127.0.0.1:9000/webhook',
    },
    conversations: [
      {
        id: 'conv-1',
        title: '整体状态怎么样',
        created_at: NOW() - 3600,
        updated_at: NOW() - 3500,
        messages: [
          { role: 'user', content: '现在校园网整体状态怎么样？', created_at: NOW() - 3600 },
          {
            role: 'assistant',
            content: '## 结论\n\n当前整体运行正常，5 台设备在线。\n\n- Core-3650 正常\n- 无活跃告警',
            created_at: NOW() - 3590,
          },
        ],
      },
      {
        id: 'conv-2',
        title: '教学楼连接不上排查',
        created_at: NOW() - 7200,
        updated_at: NOW() - 7100,
        messages: [
          { role: 'user', content: '教学楼连接不上，帮我排查原因。', created_at: NOW() - 7200 },
          { role: 'assistant', content: '## 根因\n\nPC-JX1 主机离线。', created_at: NOW() - 7190 },
        ],
      },
      {
        id: 'conv-3',
        title: '带宽是否拥塞',
        created_at: NOW() - 86400,
        updated_at: NOW() - 86300,
        messages: [
          { role: 'user', content: '当前带宽是否拥塞？', created_at: NOW() - 86400 },
          { role: 'assistant', content: '## 结论\n\n无拥塞，利用率峰值 45%。', created_at: NOW() - 86390 },
        ],
      },
    ],
    actions: {
      'act-clear-alerts': {
        id: 'act-clear-alerts',
        type: 'clear_cleared_alerts',
        params: { before: null },
        confirm_text: '将清空全部已恢复告警，确认执行？',
        danger: true,
        status: 'pending',
      },
      'act-confirm-device': {
        id: 'act-confirm-device',
        type: 'confirm_scene_device',
        params: { name: 'AP-LAB-1' },
        confirm_text: '确认新设备 AP-LAB-1 加入实验楼？',
        danger: false,
        status: 'pending',
      },
    },
    sceneBuildings: [
      {
        id: 'core',
        name: '中心机房',
        position: { x: 0, z: 0 },
        size: { width: 48, depth: 36, height: 16 },
        style: { color: '#2f6f9e' },
        status: 'confirmed',
        devices: [
          { name: 'Core-3650', type: 'switch', status: 'confirmed' },
          { name: 'Router-4331', type: 'router', status: 'confirmed' },
          { name: 'Server-1', type: 'server', status: 'confirmed' },
        ],
      },
      {
        id: 'teach',
        name: '教学楼',
        position: { x: -120, z: 40 },
        size: { width: 78, depth: 30, height: 24 },
        style: { color: '#2a8a7e' },
        status: 'confirmed',
        devices: [
          { name: 'SW-JX-2960', type: 'switch', status: 'confirmed' },
          { name: 'PC-JX1', type: 'pc', status: 'confirmed' },
          { name: 'PC-JX2', type: 'pc', status: 'confirmed' },
        ],
      },
      {
        id: 'train',
        name: '实训楼',
        position: { x: 120, z: 40 },
        size: { width: 62, depth: 30, height: 20 },
        style: { color: '#7a63b6' },
        status: 'confirmed',
        devices: [
          { name: 'SW-SX-2960', type: 'switch', status: 'confirmed' },
          { name: 'PC-SX1', type: 'pc', status: 'confirmed' },
        ],
      },
      {
        id: 'dorm',
        name: '宿舍楼',
        position: { x: 0, z: -140 },
        size: { width: 66, depth: 26, height: 21 },
        style: { color: '#9a783f' },
        status: 'confirmed',
        devices: [
          { name: 'SW-SS-2960', type: 'switch', status: 'confirmed' },
          { name: 'PC-SS1', type: 'pc', status: 'confirmed' },
          { name: 'PC-SS2', type: 'pc', status: 'confirmed' },
        ],
      },
      {
        id: 'lab',
        name: '实验楼',
        position: { x: 150, z: -95 },
        size: { width: 54, depth: 26, height: 18 },
        style: { color: '#4e8f6b' },
        status: 'confirmed',
        devices: [
          { name: 'SW-XS-2960', type: 'switch', status: 'pending' },
          { name: 'AP-LAB-1', type: 'ap', status: 'pending' },
        ],
      },
    ],
    pending: [
      {
        name: 'AP-LAB-1',
        type: 'ap',
        building_id: 'lab',
        inferred_building: '实验楼',
        model: 'Aironet 1800',
        ip: '192.168.60.10',
      },
      {
        name: 'CAM-GATE-1',
        type: 'camera',
        building_id: '',
        inferred_building: '',
        model: 'C9300-CAM',
        ip: '192.168.60.20',
      },
    ],
  };
}

let state = seedState();

const DEVICES = [
  { id: 'd-core', hostname: 'Core-3650', management_ip: '192.168.10.1', platform_id: '3650', product_id: '3650-24PS', device_type: 'MultiLayerSwitch', software_version: '16.3.2', reachability: 'Reachable', collection_status: 'Managed', interface_count: 34, source: 'netcon' },
  { id: 'd-router', hostname: 'Router-4331', management_ip: '10.0.0.2', platform_id: 'ISR4300', product_id: 'ISR4331', device_type: 'Router', software_version: '16.6.4', reachability: 'Reachable', collection_status: 'Managed', interface_count: 5, source: 'netcon' },
  { id: 'd-jx', hostname: 'SW-JX-2960', management_ip: '192.168.10.11', platform_id: '2960', product_id: '2960-24TT', device_type: 'Switch', software_version: '15.0', reachability: 'Reachable', collection_status: 'Unreachable', interface_count: 28, source: 'netcon' },
  { id: 'd-sx', hostname: 'SW-SX-2960', management_ip: '192.168.10.12', platform_id: '2960', product_id: '2960-24TT', device_type: 'Switch', software_version: '15.0', reachability: 'Reachable', collection_status: 'Managed', interface_count: 28, source: 'netcon' },
  { id: 'd-ss', hostname: 'SW-SS-2960', management_ip: '192.168.10.13', platform_id: '2960', product_id: '2960-24TT', device_type: 'Switch', software_version: '15.0', reachability: 'Reachable', collection_status: 'Managed', interface_count: 28, source: 'netcon' },
  { id: 'd-xs', hostname: 'SW-XS-2960', management_ip: '192.168.60.11', platform_id: '2960', product_id: '2960-24TT', device_type: 'Switch', software_version: '15.0', reachability: 'Reachable', collection_status: 'Managed', interface_count: 28, source: 'netcon' },
];

const HOSTS = [
  { id: 'h-pc-jx1', hostname: 'PC-JX1', ip: '192.168.20.10', mac: '0005.5EB1.6A45', connected_device_name: 'SW-JX-2960', connected_interface: 'FastEthernet0/1', host_type: 'Pc', ping_status: 'SUCCESS', source: 'netcon' },
  { id: 'h-pc-jx2', hostname: 'PC-JX2', ip: '192.168.20.11', mac: '0002.1726.E0D7', connected_device_name: 'SW-JX-2960', connected_interface: 'FastEthernet0/2', host_type: 'Pc', ping_status: 'SUCCESS', source: 'netcon' },
  { id: 'h-pc-sx1', hostname: 'PC-SX1', ip: '192.168.30.10', mac: '0060.2FA3.478A', connected_device_name: 'SW-SX-2960', connected_interface: 'FastEthernet0/1', host_type: 'Pc', ping_status: 'SUCCESS', source: 'netcon' },
  { id: 'h-pc-ss1', hostname: 'PC-SS1', ip: '192.168.40.10', mac: '0060.3EE1.8745', connected_device_name: 'SW-SS-2960', connected_interface: 'FastEthernet0/1', host_type: 'Pc', ping_status: 'SUCCESS', source: 'netcon' },
  { id: 'h-pc-ss2', hostname: 'PC-SS2', ip: '192.168.40.11', mac: '0001.4261.BB6A', connected_device_name: 'SW-SS-2960', connected_interface: 'FastEthernet0/2', host_type: 'Pc', ping_status: 'SUCCESS', source: 'netcon' },
  { id: 'h-server-1', hostname: 'Server-1', ip: '192.168.50.10', mac: '00E0.F93C.3617', connected_device_name: 'Core-3650', connected_interface: 'GigabitEthernet1/0/5', host_type: 'Server', ping_status: 'SUCCESS', vlan_id: '50', source: 'netcon' },
];

function json(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Accept, X-Operator',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
    });
    req.on('end', () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch {
        resolve({});
      }
    });
  });
}

function audit(action, target, detail) {
  state.audit.unshift({
    id: `audit-${Date.now()}-${state.audit.length}`,
    operator: 'web-ui',
    action,
    target,
    detail: detail || '',
    time: NOW(),
  });
  state.audit = state.audit.slice(0, 100);
}

function pendingDevicesPayload() {
  return state.pending.slice();
}

function scenePayload() {
  return {
    version: state.sceneVersion,
    theme: 'campus-lowpoly',
    buildings: state.sceneBuildings.map((building) => ({ ...building, devices: building.devices.map((d) => ({ ...d })) })),
    pending: pendingDevicesPayload(),
  };
}

function agentConversationSummary(conversation) {
  return {
    id: conversation.id,
    title: conversation.title,
    created_at: conversation.created_at,
    updated_at: conversation.updated_at,
    message_count: conversation.messages.length,
  };
}

function alertSummary() {
  const active = state.alerts.filter((alert) => alert.state === 'active').length;
  const cleared = state.alerts.filter((alert) => alert.state === 'cleared').length;
  const silenced = state.alerts.filter((alert) => alert.state === 'silenced').length;
  return { active, cleared, silenced, info: 0, total: state.alerts.length };
}

// --------------------------------------------------------------------------- //
// W10：告警根因分组 / 路径追踪 / 通知 的数据构造
// --------------------------------------------------------------------------- //
function deviceNameById(id) {
  const device = DEVICES.find((item) => item.id === id);
  if (device) return device.hostname;
  const host = HOSTS.find((item) => item.id === id);
  if (host) return host.hostname;
  const sceneDevice = state.sceneBuildings
    .flatMap((building) => building.devices)
    .find((item) => item.name === id);
  return sceneDevice?.name || id;
}

function buildAlertGroups() {
  const activeAlerts = state.alerts.filter((alert) => alert.state === 'active');
  if (!activeAlerts.length) return { count: 0, groups: [] };
  const priority = { link_down: 5, device_unreachable: 4, device_offline: 3, interface_down: 2, host_down: 1 };
  const rootAlert = [...activeAlerts].sort(
    (a, b) =>
      (priority[b.alert_type] || 0) - (priority[a.alert_type] || 0) ||
      (b.severity === 'critical' ? 1 : 0) - (a.severity === 'critical' ? 1 : 0),
  )[0];
  const groupId = `g_${rootAlert.device_id || 'root'}_${rootAlert.alert_type}`;
  const members = activeAlerts.map((alert) => ({
    id: alert.id,
    alert_type: alert.alert_type,
    severity: alert.severity,
    device_name: deviceNameById(alert.device_id),
    detail: alert.detail,
    observed_at: alert.observed_at,
    suppressed_by: alert.id === rootAlert.id ? null : groupId,
  }));
  const suppressed = members.filter((member) => member.suppressed_by).length;
  return {
    count: 1,
    groups: [
      {
        group_id: groupId,
        root_cause: rootAlert.alert_type,
        root_device_id: rootAlert.device_id,
        root_device_name: deviceNameById(rootAlert.device_id),
        severity: rootAlert.severity,
        title: rootAlert.title,
        alert_ids: members.map((member) => member.id),
        member_count: members.length,
        suppressed_count: suppressed,
        first_seen: Math.min(...activeAlerts.map((alert) => alert.observed_at)),
        last_seen: Math.max(...activeAlerts.map((alert) => alert.observed_at)),
        members,
      },
    ],
  };
}

function traceHop(index, name, id, role, ingress = null, egress = null) {
  return {
    index,
    device_name: name,
    device_id: id,
    ingress_port: ingress,
    egress_port: egress,
    role,
  };
}

/** 构造 mock 路径结果：PC-JX1 → Server-1 完整；→ CAM-GATE-1 部分可达（断点） */
function buildPathTrace(sourceIp, destIp) {
  const endpointByIp = new Map();
  for (const host of HOSTS) endpointByIp.set(host.ip, { name: host.hostname, id: host.id, kind: 'host' });
  for (const device of DEVICES) {
    endpointByIp.set(device.management_ip, { name: device.hostname, id: device.id, kind: 'device' });
  }
  for (const pending of state.pending) {
    if (pending.ip) endpointByIp.set(pending.ip, { name: pending.name, id: `scene:${pending.name}`, kind: 'pending' });
  }
  const source = endpointByIp.get(sourceIp);
  const dest = endpointByIp.get(destIp);
  if (!source || !dest) {
    return {
      ok: false,
      source_ip: sourceIp,
      dest_ip: destIp,
      trace_status: 'failed',
      hops: [],
      links: [],
      elapsed_ms: 0,
      error: '源或目的地址不在当前拓扑/主机清单中',
    };
  }
  if (sourceIp === destIp) {
    return { ok: false, source_ip: sourceIp, dest_ip: destIp, trace_status: 'failed', hops: [], links: [], elapsed_ms: 0, error: '源与目的相同' };
  }
  const coreHop = traceHop(1, 'Core-3650', 'd-core', 'transit', 'GigabitEthernet1/0/6', 'GigabitEthernet1/0/1');
  const sourceHop = traceHop(0, source.name, source.id, 'source', null, 'FastEthernet0');
  // 断点场景：目的为待确认设备 CAM-GATE-1（NC 未返回完整 L3 路径）
  if (dest.name === 'CAM-GATE-1') {
    return {
      ok: true,
      source_ip: sourceIp,
      dest_ip: destIp,
      trace_status: 'partial',
      hops: [
        sourceHop,
        traceHop(1, 'SW-JX-2960', 'd-jx', 'transit', 'FastEthernet0/1', 'GigabitEthernet0/1'),
        { ...coreHop, index: 2, egress_port: null },
      ],
      links: [
        { source: source.id, target: 'd-jx', link_id: 'l-pc-jx-sw' },
        { source: 'd-jx', target: 'd-core', link_id: 'l-jx-core' },
      ],
      elapsed_ms: 1420,
      error: '目标设备 CAM-GATE-1 不可达：NC flow-analysis 未返回完整路径（最后可达节点 Core-3650）',
      failed_device: 'Core-3650',
    };
  }
  const hops = [sourceHop];
  if (!['d-core'].includes(source.id)) {
    hops.push(traceHop(hops.length, 'SW-JX-2960', 'd-jx', 'transit', 'FastEthernet0/1', 'GigabitEthernet0/1'));
  }
  hops.push({ ...coreHop, index: hops.length });
  hops.push(traceHop(hops.length, dest.name, dest.id, 'destination', 'GigabitEthernet1/0/24', null));
  return {
    ok: true,
    source_ip: sourceIp,
    dest_ip: destIp,
    trace_status: 'complete',
    hops,
    links: hops.slice(1).map((hop, index) => ({
      source: hops[index].device_id,
      target: hop.device_id,
      link_id: `l-${index}`,
    })),
    elapsed_ms: 1680,
    error: null,
  };
}

// --------------------------------------------------------------------------- //
// W12：RAG 知识库 / 回答 / trace（非流式与流式共用）
// --------------------------------------------------------------------------- //
function mockRagHits() {
  return [
    {
      id: 'K1',
      title: '校园网 2960 接入交换机配置规范',
      source: 'docs/校园网运维手册.md#L42',
      score: 0.93,
      snippet: '接入层 2960 使用 VLAN 20（教学楼）/ 30（实训楼）/ 40（宿舍楼），上联 Core-3650 Gi1/0/x。',
    },
    {
      id: 'K2',
      title: '实验楼新增设备接入流程（NC 自动发现）',
      source: 'docs/W9-Agent扩展设计.md#L88',
      score: 0.87,
      snippet: '新设备在 PT 上电后由后端自动发现为 pending，需在 3D 场景中确认后纳管，确认前不参与告警统计。',
    },
    {
      id: 'K3',
      title: '告警生命周期与静默/删除规范',
      source: 'docs/W3-告警设计.md#L12',
      score: 0.74,
      snippet: '活跃告警不可删除、只能静默；已恢复告警可软删除并支持 10 秒内撤销，写审计日志。',
    },
  ];
}

function buildMockAnswer(question = '') {
  const ragHits = mockRagHits();
  if (/清空|清除/.test(question) && /告警/.test(question)) {
    return {
      answer: `## 操作待确认：清空已恢复告警\n\n当前有 2 条已恢复告警可清理 [K3]。\n\n- 活跃告警不会被删除，只能静默 [K3]\n- 删除为软删除，支持 10 分钟撤销\n\n> 确认执行前不会修改任何数据。`,
      actions: [{ ...state.actions['act-clear-alerts'] }],
      citations: ragHits.filter((hit) => hit.id === 'K3'),
    };
  }
  if (/宿舍楼|2960|接入交换机/.test(question)) {
    return {
      answer: `## 宿舍楼新增 2960 接入交换机方案\n\n### 一、配置步骤\n\n1. 将交换机上电并连接宿舍楼接入段 [K1]\n2. 在 PT 中确认设备被自动发现为 pending 状态 [K2]\n3. 场景确认后将设备纳入纳管（VLAN 40）[K1]\n\n### 二、参考命令\n\n\`\`\`cisco\nenable\nconfigure terminal\nhostname SW-SS-2961\ninterface vlan 40\nip address 192.168.40.1 255.255.255.0\n\`\`\`\n\n> 需要在场景中确认新设备（待确认操作卡片）。`,
      actions: [{ ...state.actions['act-confirm-device'] }],
      citations: ragHits,
    };
  }
  return {
    answer: `## 根因\n\n**mock 分析结论**：当前校园网整体运行正常，5 台设备在线；实验楼有新设备待确认。检索到 ${ragHits.length} 条相关知识 [K1][K2]。\n\n## 依据\n\n- 设备清单：Core-3650 / SW-JX-2960 / SW-SX-2960 / SW-SS-2960 / Router-4331 [K1]\n- 告警：已恢复 2 条，活跃 1 条（SW-JX-2960 采集异常）[K3]\n\n## 建议\n\n1. 确认新设备 AP-LAB-1 加入实验楼 [K2]\n2. 清理已恢复告警历史 [K3]\n\n\`\`\`cisco\nshow ip interface brief\n\`\`\``,
    actions: Object.values(state.actions).map((action) => ({ ...action })),
    citations: ragHits,
  };
}

function buildMockTrace(question, answer, citations, actionsCount = 0) {
  return {
    question,
    provider: 'mock',
    model: 'mock-rules',
    mode: 'multi_agent',
    roles: ['main', 'recon', 'steward'],
    retrieval: { query: question, hits: citations },
    plan: [
      {
        role: 'recon',
        label: '检索知识库 & 采集数据',
        objective: 'RAG 检索 + 读取后端实时数据',
        ok: true,
        elapsed_ms: 36,
        tools_used: ['get_health', 'get_devices', 'get_alerts'],
        actions: actionsCount,
      },
    ],
    subtask_count: 1,
    tool_sequence: ['get_health', 'get_devices', 'get_alerts'],
    tool_call_count: 3,
    tool_error_count: 0,
    actions_count: actionsCount,
    stop_reason: 'final_answer',
    polish: { used: true, elapsed_ms: 20 },
    elapsed_ms: 86,
  };
}

// --------------------------------------------------------------------------- //
// W15：可观测性（traces / metrics / RAG 评测）mock 数据
// --------------------------------------------------------------------------- //
function w15Traces() {
  const tools = ['get_health', 'get_devices', 'get_alerts', 'get_issues', 'get_hosts', 'get_topology', 'get_device_detail', 'trace_path'];
  const eventNames = ['tool_call', 'model_response', 'tool_call', 'final_answer'];
  const baseTs = NOW() - 900;
  const conversationIds = state.conversations.length
    ? state.conversations.map((conversation) => conversation.id)
    : ['conv-1', 'conv-2', 'conv-3'];
  const items = [];
  for (let index = 0; index < 24; index += 1) {
    const ok = index % 7 !== 3;
    items.push({
      trace_id: `tr-${1000 + index}`,
      conversation_id: conversationIds[index % conversationIds.length],
      step: (index % 4) + 1,
      event: eventNames[index % eventNames.length],
      tool: tools[index % tools.length],
      provider: 'mock',
      model: 'mock-rules',
      tokens_in: 700 + index * 13,
      tokens_out: 180 + index * 7,
      latency_ms: 120 + ((index * 37) % 900),
      ok,
      error: ok ? '' : `mock tool error #${index}`,
      ts: baseTs + index * 22,
    });
  }
  return items;
}

function w15TraceDetail(traceId) {
  const items = w15Traces();
  const item = items.find((trace) => trace.trace_id === traceId) || items[0];
  const steps = [
    { step: 1, event: 'tool_call', tool: item.tool, latency_ms: 42, ok: true, tokens_in: item.tokens_in, tokens_out: 0, ts: item.ts },
    { step: 2, event: 'model_response', tool: '', latency_ms: 63, ok: true, tokens_in: 0, tokens_out: 0, ts: item.ts + 20 },
    { step: 3, event: 'final_answer', tool: '', latency_ms: 31, ok: item.ok, error: item.error, tokens_in: 0, tokens_out: item.tokens_out, ts: item.ts + 40 },
  ];
  return {
    trace_id: item.trace_id,
    conversation_id: item.conversation_id,
    latency_ms: item.latency_ms,
    tokens_in: item.tokens_in,
    tokens_out: item.tokens_out,
    tool_calls: item.tool ? 1 : 0,
    ok: item.ok,
    error: item.error,
    steps,
  };
}

function w15Metrics() {
  const items = w15Traces();
  const calls = items.length;
  const avgLatency = items.reduce((sum, item) => sum + item.latency_ms, 0) / calls;
  const errors = items.filter((item) => !item.ok).length;
  const tokensTotal = items.reduce((sum, item) => sum + item.tokens_in + item.tokens_out, 0);
  return {
    avg_latency_ms: Math.round(avgLatency * 10) / 10,
    tool_error_rate: Math.round((errors / calls) * 1000) / 1000,
    tokens_total: tokensTotal,
    calls,
    by_day: [
      { date: '2026-10-29', calls: 12, tokens: Math.round(tokensTotal * 0.42), errors: 2 },
      { date: '2026-10-30', calls: calls - 12, tokens: Math.round(tokensTotal * 0.58), errors: errors - 2 },
    ],
    by_model: [
      { model: 'mock-rules', calls, tokens: tokensTotal, avg_latency_ms: Math.round(avgLatency) },
      { model: 'deepseek-flash', calls: 6, tokens: Math.round(tokensTotal * 0.6), avg_latency_ms: 780 },
    ],
  };
}

function w15RagReport() {
  return {
    recall_at_5: 0.86,
    mrr: 0.74,
    citation_accuracy: 0.91,
    faithfulness: 0.88,
    samples: 24,
    generated_at: NOW(),
    source: 'mock',
  };
}

// --------------------------------------------------------------------------- //
// Backend mock
// --------------------------------------------------------------------------- //
const backendServer = createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${BACKEND_PORT}`);
  const path = url.pathname;
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Accept, X-Operator',
    });
    res.end();
    return;
  }

  // 测试辅助：模拟 PT 新增设备被自动发现
  if (path === '/__mock/add_pending' && req.method === 'POST') {
    const name = url.searchParams.get('name') || `AP-NEW-${state.sceneVersion}`;
    const buildingId = url.searchParams.get('building_id') || 'lab';
    const building = state.sceneBuildings.find((item) => item.id === buildingId);
    if (building) {
      building.devices.push({ name, type: 'ap', status: 'pending' });
      state.pending.push({
        name,
        type: 'ap',
        building_id: buildingId,
        inferred_building: building.name,
        model: 'Aironet 1800',
        ip: '192.168.60.99',
      });
    }
    state.sceneVersion += 1;
    json(res, 200, { ok: true, version: state.sceneVersion, pending: pendingDevicesPayload() });
    return;
  }
  // W10 测试辅助：注入一组“链路中断 + 下游被抑制”的活跃告警，用于根因分组视图
  if (path === '/__mock/seed_w10' && req.method === 'POST') {
    if (!state.alerts.some((alert) => alert.id === 201)) {
      state.alerts.push(
        {
          id: 201,
          alert_type: 'link_down',
          severity: 'critical',
          title: '上联链路中断：Core-3650 ↔ SW-JX-2960',
          detail: 'Gi1/0/6 linkStatus=down；下游 SW-JX-2960 / PC-JX1 受影响',
          dedup_key: 'link_down:core-jx',
          device_id: 'd-core',
          observed_at: NOW() - 30,
          source_ts: '2026-10-30 19:20:00',
          state: 'active',
          resolved_at: null,
          clear_reason: null,
          stale_source: false,
          source: 'netcon',
        },
        {
          id: 202,
          alert_type: 'device_unreachable',
          severity: 'warning',
          title: '设备不可达：SW-JX-2960（受上游链路影响）',
          detail: 'collectionStatus=Unreachable（被上游 link_down 抑制）',
          dedup_key: 'device_unreachable:d-jx-w10',
          device_id: 'd-jx',
          observed_at: NOW() - 25,
          source_ts: '2026-10-30 19:20:10',
          state: 'active',
          resolved_at: null,
          clear_reason: null,
          stale_source: false,
          source: 'netcon',
        },
        {
          id: 203,
          alert_type: 'host_down',
          severity: 'critical',
          title: '主机离线：PC-JX1（受上游链路影响）',
          detail: 'pingStatus=FAILURE（被上游 link_down 抑制）',
          dedup_key: 'host_down:h-pc-jx1-w10',
          device_id: 'h-pc-jx1',
          observed_at: NOW() - 20,
          source_ts: '2026-10-30 19:20:20',
          state: 'active',
          resolved_at: null,
          clear_reason: null,
          stale_source: false,
          source: 'netcon',
        },
      );
    }
    json(res, 200, { ok: true, alerts: alertSummary(), groups: buildAlertGroups() });
    return;
  }
  if (path === '/__mock/reset' && req.method === 'POST') {
    state = seedState();
    json(res, 200, { ok: true });
    return;
  }

  if (path === '/api/health') {
    json(res, 200, {
      status: 'ok',
      version: 'w9-mock',
      server_time: new Date().toISOString(),
      driver: 'mock',
      dataset_source: 'mock',
      stale: false,
      polls: state.sceneVersion,
      last_success_at: new Date().toISOString(),
      hosts_observed_at: new Date().toISOString(),
      counts: { devices: DEVICES.length, links: 5, hosts: HOSTS.length, issues: 0 },
      detection: { engine: 'self-rules-v1', rules: ['host_down', 'device_unreachable', 'link_down'], alert_states: alertSummary() },
      driver_status: { driver: 'mock', reachable: true, base_url: 'mock://w9' },
    });
    return;
  }
  if (path === '/api/devices') {
    json(res, 200, {
      source: 'mock',
      stale: false,
      captured_at: NOW(),
      captured_at_iso: new Date().toISOString(),
      count: DEVICES.length,
      devices: DEVICES.map((device) => ({ ...device, last_updated: '2026-10-30 17:00:00', last_seen: NOW() })),
    });
    return;
  }
  if (path === '/api/hosts') {
    json(res, 200, {
      source: 'mock',
      stale: false,
      count: HOSTS.length,
      observed_at: NOW(),
      observed_at_iso: new Date().toISOString(),
      snapshot_age_seconds: 0.8,
      hosts: HOSTS.map((host) => ({ ...host, last_updated: '2026-10-30 17:00:00', last_seen: NOW() })),
    });
    return;
  }
  if (path === '/api/issues') {
    json(res, 200, { source: 'mock', stale: false, observed_at: NOW(), count: 0, issues: [] });
    return;
  }
  if (path === '/api/network-health') {
    json(res, 200, {
      source: 'mock',
      stale: false,
      network_health: {
        total_devices: 6,
        healthy_devices: 6,
        unhealthy_devices: 0,
        unreachable_devices: 0,
        healthy_devices_percentage: 100,
        healthy_clients_percentage: 100,
        num_licensed_routers: 1,
        num_licensed_switches: 5,
        num_application_policies: 0,
        source: 'mock',
      },
    });
    return;
  }
  if (path === '/api/topology') {
    json(res, 200, {
      source: 'mock',
      stale: false,
      captured_at: NOW(),
      captured_at_iso: new Date().toISOString(),
      counts: { nodes: 6, links: 5 },
      nodes: DEVICES.map((device, index) => ({ id: device.id, x: 200 + index * 120, y: 220, label: device.hostname, source: 'mock' })),
      links: [
        { id: 'l1', source: 'd-core', target: 'd-router', start_port_name: 'Gi1/0/6', end_port_name: 'Gi0/0/0', start_port_speed: '1000000', end_port_speed: '1000000', link_status: '1', data_source: 'mock' },
        { id: 'l2', source: 'd-core', target: 'd-jx', started: false, link_status: '1', data_source: 'mock' },
        { id: 'l3', source: 'd-core', target: 'd-sx', link_status: '1', data_source: 'mock' },
        { id: 'l4', source: 'd-core', target: 'd-ss', link_status: '1', data_source: 'mock' },
        { id: 'l5', source: 'd-core', target: 'd-xs', link_status: '1', data_source: 'mock' },
      ],
    });
    return;
  }
  if (path === '/api/alerts/summary') {
    json(res, 200, alertSummary());
    return;
  }
  if (path === '/api/alerts/groups' && req.method === 'GET') {
    json(res, 200, buildAlertGroups());
    return;
  }
  if (path === '/api/notify/status') {
    json(res, 200, state.notify);
    return;
  }
  if (path === '/api/notify/test' && req.method === 'POST') {
    state.notify.sent_count += 1;
    state.notify.last_sent_at = NOW();
    state.notify.last_error = null;
    json(res, 200, {
      ok: true,
      sent: true,
      message: '测试通知已发送（webhook HTTP 200）',
      status: state.notify,
    });
    return;
  }
  if (path === '/api/path-trace' && req.method === 'POST') {
    const payload = await readBody(req);
    json(res, 200, buildPathTrace(String(payload.source_ip || ''), String(payload.dest_ip || '')));
    return;
  }
  if (path === '/api/alerts/clear' && req.method === 'POST') {
    await readBody(req);
    const cleared = state.alerts.filter((alert) => alert.state === 'cleared');
    state.alerts = state.alerts.filter((alert) => alert.state !== 'cleared');
    state.deletedAlerts.push(...cleared);
    audit('clear_alerts', 'cleared', `${cleared.length} 条`);
    json(res, 200, { ok: true, deleted: cleared.length, summary: alertSummary() });
    return;
  }
  if (path.startsWith('/api/alerts/') && req.method === 'POST') {
    const parts = path.split('/').filter(Boolean); // api alerts <id> <action>
    const id = Number(parts[2]);
    const action = parts[3];
    await readBody(req);
    if (action === 'restore') {
      const deletedIndex = state.deletedAlerts.findIndex((item) => item.id === id);
      if (deletedIndex >= 0) {
        const [restored] = state.deletedAlerts.splice(deletedIndex, 1);
        state.alerts.push(restored);
        audit('restore_alert', String(id), restored.title);
        json(res, 200, { ok: true, restored: 1, alert: restored, summary: alertSummary() });
        return;
      }
      const existing = state.alerts.find((item) => item.id === id);
      json(res, existing ? 200 : 404, existing
        ? { ok: true, alert: existing, summary: alertSummary() }
        : { detail: '告警不存在或未被删除' });
      return;
    }
    const alert = state.alerts.find((item) => item.id === id);
    if (!alert) {
      json(res, 404, { detail: '告警不存在或已删除' });
      return;
    }
    if (action === 'silence') {
      alert.state = 'silenced';
      audit('silence_alert', String(id), alert.title);
      json(res, 200, { ok: true, alert, summary: alertSummary() });
      return;
    }
    json(res, 404, { detail: '未知告警操作' });
    return;
  }
  if (path.startsWith('/api/alerts/') && req.method === 'DELETE') {
    const parts = path.split('/').filter(Boolean);
    const id = Number(parts[2]);
    const index = state.alerts.findIndex((item) => item.id === id);
    if (index < 0) {
      json(res, 404, { detail: '告警不存在或已删除' });
      return;
    }
    const [removed] = state.alerts.splice(index, 1);
    if (removed.state === 'active') {
      state.alerts.splice(index, 0, removed);
      json(res, 409, { detail: '活跃告警不可删除，请使用静默' });
      return;
    }
    state.deletedAlerts.push(removed);
    audit('delete_alert', String(id), removed.title);
    json(res, 200, { ok: true, deleted: 1, id, summary: alertSummary() });
    return;
  }
  if (path === '/api/alerts') {
    const limit = Number(url.searchParams.get('limit') || 50);
    json(res, 200, {
      count: Math.min(limit, state.alerts.length),
      state: url.searchParams.get('state') || 'all',
      summary: alertSummary(),
      alerts: state.alerts.slice(0, limit),
    });
    return;
  }
  if (path === '/api/detection/rules') {
    json(res, 200, { engine: 'self-rules-v1', rules: [{ rule_id: 'host_down' }, { rule_id: 'device_unreachable' }, { rule_id: 'link_down' }], alert_states: alertSummary(), notes: ['mock'] });
    return;
  }
  if (path === '/api/scene') {
    json(res, 200, scenePayload());
    return;
  }
  if (path === '/api/scene/pending') {
    json(res, 200, { count: state.pending.length, devices: pendingDevicesPayload() });
    return;
  }
  if (path === '/api/audit') {
    json(res, 200, { count: state.audit.length, audit: state.audit });
    return;
  }
  if (path.startsWith('/api/scene/devices/') && req.method === 'POST' && path.endsWith('/confirm')) {
    const name = decodeURIComponent(path.split('/').filter(Boolean)[3] || '');
    for (const building of state.sceneBuildings) {
      const device = building.devices.find((item) => item.name === name);
      if (device) device.status = 'confirmed';
    }
    state.pending = state.pending.filter((item) => item.name !== name);
    state.sceneVersion += 1;
    audit('confirm_scene_device', name);
    json(res, 200, { ok: true, name, scene: scenePayload() });
    return;
  }
  if (path.startsWith('/api/scene/devices/') && req.method === 'DELETE') {
    const name = decodeURIComponent(path.split('/').filter(Boolean)[3] || '');
    for (const building of state.sceneBuildings) {
      building.devices = building.devices.filter((item) => item.name !== name);
    }
    state.pending = state.pending.filter((item) => item.name !== name);
    state.sceneVersion += 1;
    audit('remove_scene_device', name);
    json(res, 200, { ok: true, name, scene: scenePayload() });
    return;
  }
  if (path.match(/^\/api\/scene\/buildings\/[^/]+\/devices$/) && req.method === 'POST') {
    const buildingId = decodeURIComponent(path.split('/')[4]);
    const payload = await readBody(req);
    const building = state.sceneBuildings.find((item) => item.id === buildingId);
    if (!building) {
      json(res, 404, { detail: '楼栋不存在' });
      return;
    }
    const name = payload.name || payload.hostname;
    const existing = building.devices.find((item) => item.name === name);
    if (existing) {
      Object.assign(existing, payload, { status: payload.status || existing.status });
    } else {
      building.devices.push({ name, type: payload.type || 'unknown', status: payload.status || 'pending' });
    }
    if (!state.pending.some((item) => item.name === name)) {
      state.pending.push({ name, type: payload.type || 'unknown', building_id: buildingId, inferred_building: building.name, model: payload.model || '', ip: payload.ip || '' });
    }
    state.sceneVersion += 1;
    audit('upsert_scene_device', `${buildingId}/${name}`);
    json(res, 200, { ok: true, scene: scenePayload() });
    return;
  }
  json(res, 404, { detail: `mock backend: 未实现 ${req.method} ${path}` });
});

// --------------------------------------------------------------------------- //
// Agent mock
// --------------------------------------------------------------------------- //
const agentServer = createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${AGENT_PORT}`);
  const path = url.pathname;
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Accept, X-Operator',
    });
    res.end();
    return;
  }
  if (path === '/api/health') {
    json(res, 200, {
      status: 'ok',
      service: 'campus-twin-agent',
      version: 'w9-mock',
      provider: 'mock',
      model: 'mock-rules',
      provider_ready: true,
      provider_error: null,
      data_source: 'http',
      backend: { mode: 'http', base_url: `http://127.0.0.1:${BACKEND_PORT}`, reachable: true },
      max_steps: 6,
    });
    return;
  }
  if (path === '/api/conversations' && req.method === 'GET') {
    json(res, 200, { count: state.conversations.length, conversations: state.conversations.map(agentConversationSummary) });
    return;
  }
  if (path === '/api/conversations/clear' && req.method === 'POST') {
    state.conversations = [];
    json(res, 200, { ok: true, cleared: true });
    return;
  }
  if (path.startsWith('/api/conversations/') && req.method === 'GET') {
    const id = decodeURIComponent(path.split('/').filter(Boolean)[2]);
    const conversation = state.conversations.find((item) => item.id === id);
    if (!conversation) {
      json(res, 404, { detail: '会话不存在' });
      return;
    }
    json(res, 200, {
      conversation: { id: conversation.id, title: conversation.title },
      messages: conversation.messages.map((message) => ({ role: message.role, content: message.content, created_at: message.created_at, meta: message.meta || {} })),
    });
    return;
  }
  if (path.startsWith('/api/conversations/') && req.method === 'DELETE') {
    const id = decodeURIComponent(path.split('/').filter(Boolean)[2]);
    state.conversations = state.conversations.filter((item) => item.id !== id);
    json(res, 200, { ok: true, deleted: id });
    return;
  }
  if (path === '/__mock/stream_mode' && req.method === 'POST') {
    const payload = await readBody(req);
    state.streamMode =
      payload.mode === '404' || payload.mode === 'error' ? payload.mode : 'ok';
    json(res, 200, { ok: true, mode: state.streamMode });
    return;
  }
  // W15：可观测性接口模式（ok / 404 / no_rag）
  if (path === '/__mock/w15_mode' && req.method === 'POST') {
    const payload = await readBody(req);
    state.w15Mode =
      payload.mode === '404' || payload.mode === 'no_rag' ? payload.mode : 'ok';
    json(res, 200, { ok: true, mode: state.w15Mode });
    return;
  }
  if (path === '/api/metrics/agent' && req.method === 'GET') {
    if (state.w15Mode === '404') {
      json(res, 404, { detail: 'Not Found' });
      return;
    }
    json(res, 200, w15Metrics());
    return;
  }
  if (path === '/api/traces' && req.method === 'GET') {
    if (state.w15Mode === '404') {
      json(res, 404, { detail: 'Not Found' });
      return;
    }
    const conversationId = url.searchParams.get('conversation_id');
    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit')) || 20));
    const items = w15Traces()
      .filter((item) => !conversationId || item.conversation_id === conversationId)
      .sort((a, b) => b.ts - a.ts)
      .slice(0, limit);
    json(res, 200, { count: items.length, items });
    return;
  }
  if (path.startsWith('/api/traces/') && req.method === 'GET') {
    if (state.w15Mode === '404') {
      json(res, 404, { detail: 'Not Found' });
      return;
    }
    const traceId = decodeURIComponent(path.split('/').filter(Boolean)[2]);
    json(res, 200, w15TraceDetail(traceId));
    return;
  }
  if (path === '/api/rag/report' && req.method === 'GET') {
    if (state.w15Mode === '404' || state.w15Mode === 'no_rag') {
      json(res, 404, { detail: 'Not Found' });
      return;
    }
    json(res, 200, w15RagReport());
    return;
  }
  if (path === '/api/chat' && req.method === 'POST') {
    const payload = await readBody(req);
    const question = String(payload.question || '');
    let conversationId = payload.conversation_id;
    if (!conversationId) {
      conversationId = `conv-${Date.now()}`;
      state.conversations.unshift({
        id: conversationId,
        title: question.slice(0, 24) || '新会话',
        created_at: NOW(),
        updated_at: NOW(),
        messages: [],
      });
    }
    const conversation = state.conversations.find((item) => item.id === conversationId);
    const built = buildMockAnswer(question);
    const actions = built.actions;
    const citations = built.citations;
    const trace = buildMockTrace(question, built.answer, citations, actions.length);
    conversation.messages.push(
      { role: 'user', content: question, created_at: NOW() },
      {
        role: 'assistant',
        content: built.answer,
        created_at: NOW(),
        meta: { actions, citations },
      },
    );
    conversation.updated_at = NOW();
    json(res, 200, {
      answer: built.answer,
      trace,
      conversation_id: conversationId,
      actions,
      citations,
      provider: 'mock',
      model: 'mock-rules',
      elapsed_ms: 86,
    });
    return;
  }
  // W12：SSE 流式接口（契约事件：meta/status/tool/token/action/done/error）
  if (path === '/api/chat/stream' && req.method === 'POST') {
    const payload = await readBody(req);
    if (state.streamMode === '404') {
      json(res, 404, { detail: 'Not Found（mock：/api/chat/stream 未实现）' });
      return;
    }
    const question = String(payload.question || '');
    let conversationId = payload.conversation_id;
    if (!conversationId) {
      conversationId = `conv-${Date.now()}-stream`;
      state.conversations.unshift({
        id: conversationId,
        title: question.slice(0, 24) || '新会话',
        created_at: NOW(),
        updated_at: NOW(),
        messages: [],
      });
    }
    const conversation = state.conversations.find((item) => item.id === conversationId);
    const built = buildMockAnswer(question);
    const actions = built.actions;
    const citations = built.citations;
    const trace = buildMockTrace(question, built.answer, citations, actions.length);
    conversation.messages.push(
      { role: 'user', content: question, created_at: NOW() },
      {
        role: 'assistant',
        content: built.answer,
        created_at: NOW(),
        meta: { actions, citations, streaming: true },
      },
    );
    conversation.updated_at = NOW();

    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      'Access-Control-Allow-Origin': '*',
      'X-Accel-Buffering': 'no',
    });
    const sendEvent = (event) => {
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    };
    const sleepMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    sendEvent({
      type: 'meta',
      conversation_id: conversationId,
      provider: 'mock',
      model: 'mock-rules',
      engine: 'multi_agent',
    });
    sendEvent({ type: 'status', stage: 'classify', label: '正在理解问题…' });
    await sleepMs(60);
    sendEvent({ type: 'status', stage: 'retrieve', label: '正在检索知识库…' });
    await sleepMs(90);
    sendEvent({
      type: 'status',
      stage: 'retrieve_done',
      label: `检索到 ${citations.length} 条知识，准备引用…`,
    });
    await sleepMs(50);
    sendEvent({
      type: 'status',
      stage: 'tool',
      label: '正在调用工具 get_health / get_devices / get_alerts…',
    });
    for (const [index, toolName] of ['get_health', 'get_devices', 'get_alerts'].entries()) {
      sendEvent({ type: 'tool', name: toolName, ok: true, elapsed_ms: 8 + index * 4 });
      await sleepMs(40);
    }
    sendEvent({ type: 'status', stage: 'aggregate', label: '正在生成回答…' });
    if (state.streamMode === 'error') {
      sendEvent({ type: 'error', message: 'mock：流式生成中断（模拟 SSE error 事件）' });
      res.end();
      return;
    }
    const answer = built.answer;
    for (let index = 0; index < answer.length; index += 6) {
      sendEvent({ type: 'token', text: answer.slice(index, index + 6) });
      await sleepMs(18);
    }
    for (const action of actions) sendEvent({ type: 'action', action });
    sendEvent({
      type: 'done',
      answer,
      trace,
      elapsed_ms: Math.max(120, Math.ceil(answer.length / 6) * 18),
      conversation_id: conversationId,
      actions,
      citations,
      provider: 'mock',
      model: 'mock-rules',
    });
    res.end();
    return;
  }
  if (path.match(/^\/api\/actions\/[^/]+\/confirm$/)) {
    const id = decodeURIComponent(path.split('/').filter(Boolean)[2]);
    const action = state.actions[id];
    if (!action) {
      json(res, 404, { detail: 'action 不存在' });
      return;
    }
    if (action.type === 'clear_cleared_alerts') {
      const cleared = state.alerts.filter((alert) => alert.state === 'cleared');
      state.alerts = state.alerts.filter((alert) => alert.state !== 'cleared');
      action.status = 'done';
      json(res, 200, { ok: true, message: `已清空 ${cleared.length} 条已恢复告警`, result: { deleted: cleared.length } });
      return;
    }
    if (action.type === 'confirm_scene_device') {
      const name = action.params?.name || '';
      for (const building of state.sceneBuildings) {
        const device = building.devices.find((item) => item.name === name);
        if (device) device.status = 'confirmed';
      }
      state.pending = state.pending.filter((item) => item.name !== name);
      state.sceneVersion += 1;
      action.status = 'done';
      json(res, 200, { ok: true, message: `已确认新设备 ${name}`, result: { name } });
      return;
    }
    action.status = 'done';
    json(res, 200, { ok: true, message: '操作已执行' });
    return;
  }
  if (path.match(/^\/api\/actions\/[^/]+\/cancel$/)) {
    const id = decodeURIComponent(path.split('/').filter(Boolean)[2]);
    if (state.actions[id]) state.actions[id].status = 'cancelled';
    json(res, 200, { ok: true, message: '操作已取消' });
    return;
  }
  json(res, 404, { detail: `mock agent: 未实现 ${req.method} ${path}` });
});

backendServer.listen(BACKEND_PORT, '127.0.0.1', () => {
  console.log(`[mock-w9] backend  -> http://127.0.0.1:${BACKEND_PORT}`);
});
agentServer.listen(AGENT_PORT, '127.0.0.1', () => {
  console.log(`[mock-w9] agent    -> http://127.0.0.1:${AGENT_PORT}`);
});
console.log('[mock-w9] ready（Ctrl+C 退出）');
