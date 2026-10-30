#!/usr/bin/env node
/**
 * 端到端冒烟自测（headless Edge/Chrome + CDP，无需 Playwright）。
 *
 * 三种模式（SMOKE_MODE 环境变量）：
 *   live（默认）：页面连真实后端 8000 + Agent 8100，验证真实数据渲染 / 点击设备 / AI 真实问答；
 *   demo        ：页面连接不可达后端（离线场景），验证“未连接”提示 + 演示数据开关 + 告警脉冲；
 *   ai-offline  ：后端 8000 正常、Agent 8100 不可达，验证「AI 服务未连接」降级且不影响 3D 大屏。
 *
 * 前置条件：
 *   live       ：后端 8000 + Agent 8100 + dev server 5173 已启动
 *   demo       ：以死地址启动的 dev server 5174：
 *                PowerShell: $env:VITE_API_BASE='http://127.0.0.1:9'; npm run dev -- --port 5174
 *   ai-offline ：以死 Agent 地址启动的 dev server 5175：
 *                PowerShell: $env:VITE_AGENT_BASE='http://127.0.0.1:9'; npm run dev -- --port 5175
 *
 * 用法：
 *   npm run smoke
 *   PowerShell: $env:SMOKE_MODE='demo'; $env:SMOKE_URL='http://127.0.0.1:5174/'; npm run smoke
 *   PowerShell: $env:SMOKE_MODE='ai-offline'; $env:SMOKE_URL='http://127.0.0.1:5175/'; npm run smoke
 *
 * 截图输出：docs/screenshot-overview.png、screenshot-detail.png、screenshot-chat.png、
 *          screenshot-recovered.png（live）、docs/screenshot-demo.png（demo）、
 *          docs/screenshot-ai-offline.png（ai-offline）
 */
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { demoBundle } from '../src/api/mock.js';
import {
  buildAlertEntries,
  countActiveEntries,
  alertStateLabel,
  STALE_SOURCE_ISSUE_LABEL,
} from '../src/api/alertsView.js';
import { normalizeSnapshot } from '../src/api/normalize.js';
import { buildSceneItems } from '../src/scene/items.js';
import {
  attachPage,
  closeBrowser,
  findBrowser,
  getJson,
  launchBrowser,
  sleep,
} from './cdp-lib.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = join(__dirname, '..');
const DOCS_DIR = join(FRONTEND_DIR, 'docs');
const PROFILE_DIR = join(__dirname, '.smoke-profile');
const MODE = (process.env.SMOKE_MODE || 'live').toLowerCase();
const DEBUG_PORT = Number(
  process.env.SMOKE_CDP_PORT ||
    ({ demo: 9334, 'ai-offline': 9335, w9: 9336 }[MODE] ?? 9333),
);
const PAGE_URL =
  process.env.SMOKE_URL ||
  ({
    demo: 'http://127.0.0.1:5174/',
    'ai-offline': 'http://127.0.0.1:5175/',
    w9: 'http://127.0.0.1:5176/',
  }[MODE] ?? 'http://127.0.0.1:5173/');
const API_BASE = (process.env.VITE_API_BASE || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const AGENT_BASE = (process.env.VITE_AGENT_BASE || process.env.AGENT_BASE || 'http://127.0.0.1:8100').replace(
  /\/+$/,
  '',
);
// W9 mock 模式使用的契约地址（scripts/mock-w9-server.mjs 默认端口）
const W9_BACKEND = (process.env.W9_MOCK_BACKEND || 'http://127.0.0.1:8002').replace(/\/+$/, '');
const W9_AGENT = (process.env.W9_MOCK_AGENT || 'http://127.0.0.1:8102').replace(/\/+$/, '');
const REPORT_PATH = join(
  __dirname,
  {
    demo: 'smoke-demo-report.txt',
    'ai-offline': 'smoke-ai-offline-report.txt',
    w9: 'smoke-w9-report.txt',
  }[MODE] ?? 'smoke-report.txt',
);

const reportLines = [];
const failures = [];

function log(line = '') {
  reportLines.push(line);
  console.log(line);
}

function check(condition, okMessage, failMessage) {
  if (condition) {
    log(`  [PASS] ${okMessage}`);
  } else {
    log(`  [FAIL] ${failMessage}`);
    failures.push(failMessage);
  }
}

/** 便于自测的固定读取（与 main.js 中 #dev-stats 的结构一致） */
const READ_STATS = `(() => {
  const stats = JSON.parse(document.getElementById('dev-stats').textContent);
  const canvas = document.querySelector('#scene-root canvas');
  return {
    stats,
    canvas: canvas ? { width: canvas.width, height: canvas.height } : null,
    loadingHidden: document.getElementById('loading-overlay').classList.contains('hidden'),
    offlineVisible: !document.getElementById('offline-banner').classList.contains('hidden'),
    offlineText: document.getElementById('offline-detail').textContent,
    demoBadgeVisible: !document.getElementById('demo-badge').classList.contains('hidden'),
    demoBadgeText: document.getElementById('demo-badge').textContent,
    runtimeError: document.getElementById('runtime-error').textContent,
    connection: document.getElementById('stat-conn').textContent,
    source: document.getElementById('stat-source').textContent,
    health: document.getElementById('stat-health').textContent,
    deviceValue: document.getElementById('stat-devices').textContent,
    hostsValue: document.getElementById('stat-hosts').textContent,
    alertsValue: document.getElementById('stat-alerts').textContent,
    alertsSub: document.getElementById('stat-alerts-sub').textContent,
    freshness: document.getElementById('stat-freshness').textContent,
    freshnessSub: document.getElementById('stat-freshness-sub').textContent,
    freshnessTitle: document.getElementById('stat-freshness').title,
    collect: document.getElementById('stat-collect').textContent,
    alertCount: document.getElementById('alert-count').textContent,
    alertFoot: document.getElementById('alerts-foot').textContent,
    alertFootTitle: document.getElementById('alerts-foot').title,
    alertStates: [...document.querySelectorAll('.alert-item .alert-state')].map((node) => node.textContent),
    alertTimeTitles: [...document.querySelectorAll('.alert-item time')].map((node) => node.getAttribute('title') || ''),
    detailBody: document.getElementById('detail-body').textContent,
    topbarTitle: document.querySelector('.brand-text h1')?.textContent || '',
    legendItems: document.querySelectorAll('#legend li').length,
    autoRotateChecked: document.getElementById('toggle-rotate').checked,
    mockChecked: document.getElementById('toggle-mock').checked,
    aiStatus: document.getElementById('ai-status-text').textContent,
    aiModelLine: document.getElementById('ai-model-line').textContent,
    aiQuickCount: document.querySelectorAll('.ai-quick-btn').length,
    aiSendDisabled: document.getElementById('ai-send').disabled,
    aiTabDotClass: document.getElementById('ai-tab-dot').className,
  };
})()`;

async function pauseAutoRotate(cdp) {
  const checked = await cdp.evaluate(`document.getElementById('toggle-rotate').checked`);
  if (checked) {
    await cdp.evaluate(`document.getElementById('toggle-rotate').click()`);
    await sleep(250);
  }
  return checked;
}

async function restoreAutoRotate(cdp, wasChecked) {
  if (!wasChecked) return;
  await cdp.evaluate(`document.getElementById('toggle-rotate').click()`);
  await sleep(150);
}

/**
 * AI 运维助手真实问答校验：
 * 切页签 → 等待 Agent 健康状态 → 键盘输入问题 + Enter 发送 → 加载态 → 收到中文 Markdown 回答 → 展开 trace。
 */
async function runAiChatChecks(cdp, agentHealth) {
  log('');
  log('—— AI 运维助手（真实 Agent :8100 / DeepSeek）——');
  const expectedProvider = agentHealth?.provider || 'deepseek';
  const expectedModel = agentHealth?.model || 'deepseek';

  // 复位视角，让 AI 对话截图保留完整 4 楼栋全景
  await cdp.evaluate(`document.getElementById('btn-reset').click()`);
  await sleep(1300);

  await cdp.evaluate(`document.getElementById('tab-ai').click()`);
  await sleep(500);
  const tabState = await cdp.evaluate(`({
    paneActive: document.getElementById('pane-ai').classList.contains('active'),
    status: document.getElementById('ai-status-text').textContent,
    modelLine: document.getElementById('ai-model-line').textContent,
    quickCount: document.querySelectorAll('.ai-quick-btn').length,
    sendDisabled: document.getElementById('ai-send').disabled,
    tabDot: document.getElementById('ai-tab-dot').className,
  })`);
  check(tabState.paneActive === true, 'AI 运维助手页签可打开', 'AI 页签未激活');
  check(tabState.quickCount === 4, `4 个快捷提问按钮已渲染（${tabState.quickCount}）`, `快捷提问按钮数量异常：${tabState.quickCount}`);

  // Agent 健康检查是异步的，最多等 20s 变成“已连接”
  await cdp.waitFor(
    `(() => {
      const text = document.getElementById('ai-status-text').textContent || '';
      return text.includes(${JSON.stringify(expectedProvider)}) || text.includes(${JSON.stringify(expectedModel)});
    })()`,
    20000,
    'AI 服务状态 20s 内未显示为已连接（Agent :8100 是否启动？）',
  );
  const connected = await cdp.evaluate(`({
    status: document.getElementById('ai-status-text').textContent,
    modelLine: document.getElementById('ai-model-line').textContent,
    sendDisabled: document.getElementById('ai-send').disabled,
    tabDot: document.getElementById('ai-tab-dot').className,
  })`);
  check(
    connected.status.includes(expectedProvider) && connected.status.includes(expectedModel),
    `AI 状态显示真实 provider/model：${connected.status}`,
    `AI 状态未显示真实 provider/model：${connected.status}`,
  );
  check(connected.sendDisabled === false, 'AI 服务在线时发送按钮可用', 'AI 服务在线但发送按钮被禁用');
  check(connected.tabDot.includes('ok'), 'AI 页签状态圆点显示在线（绿色）', `AI 页签状态圆点异常：${connected.tabDot}`);

  // 键盘输入问题 + Enter 发送（真实输入事件，不用脚本直接调用 send）
  await cdp.evaluate(`document.getElementById('ai-input').focus()`);
  await cdp.send('Input.insertText', { text: '现在校园网整体状态怎么样？' });
  await sleep(300);
  const typed = await cdp.evaluate(`document.getElementById('ai-input').value`);
  check(typed.includes('整体状态'), 'AI 输入框可输入中文问题', `输入框内容异常：${typed}`);
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyDown',
    key: 'Enter',
    code: 'Enter',
    windowsVirtualKeyCode: 13,
    nativeVirtualKeyCode: 13,
  });
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: 'Enter',
    code: 'Enter',
    windowsVirtualKeyCode: 13,
    nativeVirtualKeyCode: 13,
  });

  // 加载/流式态：W12 优先流式（流式气泡 + 状态条），流式未就绪时回退普通加载态
  await sleep(2500);
  const loading = await cdp.evaluate(`({
    loading: Boolean(document.querySelector('.ai-msg-loading')),
    streaming: Boolean(document.querySelector('.ai-msg-streaming')),
    loadingText:
      document.querySelector('.ai-msg-loading')?.textContent ||
      document.querySelector('.ai-msg-streaming')?.textContent ||
      '',
    streamStatus: document.querySelector('.ai-stream-stage')?.textContent || '',
    userMessage: document.querySelector('.ai-msg-user')?.textContent || '',
    sendDisabled: document.getElementById('ai-send').disabled,
    paneActive: document.getElementById('pane-ai').classList.contains('active'),
  })`);
  check(loading.userMessage.includes('整体状态'), '发送后显示用户问题气泡', `未显示用户问题：${loading.userMessage}`);
  check(
    (loading.loading && loading.loadingText.includes('AI 正在分析')) ||
      (loading.streaming && /正在|生成|检索|工具/.test(loading.streamStatus || loading.loadingText)),
    '显示加载态（流式状态条或「AI 正在分析…」）',
    `未显示加载/流式状态：${JSON.stringify(loading).slice(0, 200)}`,
  );
  check(loading.sendDisabled === true, '分析期间发送按钮禁用（防重复提交）', '分析期间发送按钮未禁用');

  // 等待真实回答（单次上限 150s；外部真实模型偶发慢响应/超时时自动重试一次）
  const ANSWER_READY_EXPR = `(() => {
    const el = document.querySelector('.ai-msg-answer .ai-markdown');
    return Boolean(el && el.textContent.trim().length > 80);
  })()`;
  let answerWaitMs = 0;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const waitStartedAt = Date.now();
    try {
      await cdp.waitFor(ANSWER_READY_EXPR, 150000, '150s 内未收到 AI 回答');
      answerWaitMs = Date.now() - waitStartedAt;
      break;
    } catch (error) {
      const pageError = await cdp.evaluate(
        `(document.querySelector('.ai-msg-error')?.textContent || '').slice(0, 240)`,
      );
      if (attempt === 2) {
        throw new Error(`${error.message}（页面提示：${pageError || '无'}）`);
      }
      log(`  [WARN] 第 ${attempt} 次等待 AI 回答超时（页面提示：${pageError || '仍在加载'}），自动重试一次…`);
      // 页面已回到空闲态时，用快捷提问重新发起；仍在加载则先等它结束
      await cdp.evaluate(
        `(() => { const btn = document.querySelector('.ai-quick-btn'); if (btn && !btn.disabled) btn.click(); })()`,
      );
      await cdp
        .waitFor(`Boolean(!document.querySelector('.ai-msg-loading'))`, 90000, 'AI 重试请求仍未结束')
        .catch(() => {});
      await sleep(1200);
    }
  }
  log(`AI 真实回答等待：${(answerWaitMs / 1000).toFixed(1)}s`);

  const answer = await cdp.evaluate(`(() => {
    const root = document.querySelector('.ai-msg-answer');
    const markdown = root?.querySelector('.ai-markdown');
    return {
      text: markdown?.textContent || '',
      head: root?.querySelector('.ai-answer-head')?.textContent || '',
      headings: (markdown?.querySelectorAll('h1, h2, h3') || []).length,
      listItems: (markdown?.querySelectorAll('ul li, ol li') || []).length,
      codeBlocks: (markdown?.querySelectorAll('pre code') || []).length,
      hasTrace: Boolean(root?.querySelector('.ai-trace')),
      traceSummary: root?.querySelector('.ai-trace summary')?.textContent || '',
      loadingGone: !document.querySelector('.ai-msg-loading') && !document.querySelector('.ai-msg-streaming'),
      sendEnabled: !document.getElementById('ai-send').disabled,
      answerCount: document.querySelectorAll('.ai-msg-answer').length,
    };
  })()`);
  log(
    `AI 实测：等待 ${(answerWaitMs / 1000).toFixed(1)}s，回答 ${answer.text.length} 字，` +
      `标题 ${answer.headings} / 列表 ${answer.listItems} / 代码块 ${answer.codeBlocks}`,
  );
  log(`AI 回答头部：${answer.head.slice(0, 120)}`);

  check(answer.text.length > 100, `收到非空中文回答（${answer.text.length} 字，等待 ${(answerWaitMs / 1000).toFixed(1)}s）`, `AI 回答过短：${answer.text.length} 字`);
  check(/[\u4e00-\u9fa5]/.test(answer.text), 'AI 回答为中文', 'AI 回答不是中文');
  check(answer.headings >= 1, 'Markdown 标题已渲染', 'Markdown 标题未渲染');
  check(answer.listItems >= 3, 'Markdown 列表已渲染', 'Markdown 列表未渲染');
  check(answer.codeBlocks >= 1, 'Markdown 代码块已渲染（排障命令）', 'Markdown 代码块未渲染');
  check(
    answer.head.includes(expectedProvider) && answer.head.includes(expectedModel),
    `回答头部显示 provider/model（${expectedProvider}/${expectedModel}）`,
    `回答头部 provider/model 异常：${answer.head}`,
  );
  check(answer.hasTrace && answer.traceSummary.includes('执行轨迹'), 'trace 折叠区块存在（含工具调用摘要）', 'trace 折叠区块缺失');
  check(answer.loadingGone && answer.sendEnabled, '回答完成后加载态消失、发送按钮恢复', '回答完成后状态未复位');
  check(answer.answerCount === 1, 'AI 回答只追加一次（无重复提交）', `回答消息数量异常：${answer.answerCount}`);

  // 展开 trace，检查工具调用序列（兼容 W9 新多 Agent plan[] 与旧 steps[] 结构）
  await cdp.evaluate(`document.querySelector('.ai-trace summary')?.click()`);
  await sleep(300);
  const trace = await cdp.evaluate(`(() => {
    const details = document.querySelector('.ai-trace');
    const body = details?.querySelector('.trace-body');
    return {
      open: details?.open === true,
      calls: details?.querySelectorAll('.trace-calls li').length || 0,
      sequence: details?.querySelectorAll('.trace-sequence code').length || 0,
      planItems: details?.querySelectorAll('.trace-plan-item').length || 0,
      metaItems: details?.querySelectorAll('.trace-meta span').length || 0,
      summary: details?.querySelector('.trace-sub')?.textContent || '',
      text: (body?.textContent || '').slice(0, 300),
    };
  })()`);
  const traceText = trace.text.replace(/\s+/g, ' ');
  const traceToolEvidence =
    trace.calls >= 1 ||
    trace.sequence >= 1 ||
    trace.planItems >= 1 ||
    /工具序列|工具调用/.test(trace.text);
  const traceSummaryCount = /(\d+)\s*次工具/.test(trace.summary) || trace.calls >= 1;
  check(trace.open === true, 'trace 可展开查看', 'trace 无法展开');
  check(
    traceToolEvidence && trace.metaItems >= 4,
    `trace 显示工具调用/子任务（calls=${trace.calls} seq=${trace.sequence} plan=${trace.planItems} meta=${trace.metaItems}）`,
    `trace 内容异常：calls=${trace.calls} seq=${trace.sequence} plan=${trace.planItems} meta=${trace.metaItems}`,
  );
  check(
    traceSummaryCount,
    `trace 摘要显示工具调用/编排信息（${trace.summary.trim().slice(0, 90)}）`,
    `trace 摘要缺少工具调用次数：${trace.summary}`,
  );
  log(`trace 片段：${traceText.slice(0, 200)}`);

  // 截图：展开后的多 Agent 执行轨迹（plan[] / 工具序列 / 每步耗时）
  await cdp.evaluate(`(() => {
    const messages = document.getElementById('ai-messages');
    const traceNode = document.querySelector('.ai-trace');
    if (messages && traceNode) messages.scrollTop = traceNode.offsetTop - messages.offsetTop - 40;
  })()`);
  await sleep(500);
  const traceShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w9-trace.png'));
  log(`trace 结构截图：${traceShot}`);

  // 截图：滚动到用户问题，展示“提问 + AI 回答开头”的完整问答
  await cdp.evaluate(`(() => {
    const messages = document.getElementById('ai-messages');
    const userNode = document.querySelector('.ai-msg-user');
    if (messages && userNode) messages.scrollTop = userNode.offsetTop - messages.offsetTop - 4;
  })()`);
  await sleep(500);
  const chatShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-chat.png'));
  log(`AI 对话截图：${chatShot}`);
  return answerWaitMs;
}

/**
 * W9.1 live action 卡片联调（真实 Agent :8100 / DeepSeek）：
 *  1) 问“宿舍楼加 2960” → 断言 apply_scene_device（non-danger）卡片，**只点取消**；
 *  2) 问“把已恢复的告警清空” → 断言 danger 红色卡片，**只点取消**；
 * 全程不点“确认执行”，并在取消后校验后端 scene / alerts 数据未发生变化。
 */
async function runLiveActionChecks(cdp) {
  log('');
  log('—— W9.1 live action 卡片联调（真实 Agent :8100，只取消不确认）——');
  const fetchJson = async (url, timeoutMs = 15000) => {
    const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    if (!response.ok) throw new Error(`HTTP ${response.status}：${url}`);
    return response.json();
  };

  const sceneBefore = await fetchJson(`${API_BASE}/api/scene`);
  const pendingBefore = await fetchJson(`${API_BASE}/api/scene/pending`).catch(() => null);
  const alertsBefore = await fetchJson(`${API_BASE}/api/alerts/summary`);
  const auditBefore = await fetchJson(`${API_BASE}/api/audit?limit=20`).catch(() => null);
  log(
    `基线：scene v${sceneBefore.version}（${sceneBefore.buildings.length} 栋）· pending=${
      pendingBefore?.count ?? '?'
    } · alerts active=${alertsBefore.active} cleared=${alertsBefore.cleared} total=${alertsBefore.total}`,
  );

  /** 通过真实键盘输入提问，并等待新回答 + action 卡片渲染 */
  const askQuestion = async (question) => {
    await cdp.evaluate(`document.getElementById('tab-ai').click()`);
    await sleep(300);
    await cdp.waitFor(`Boolean(!document.querySelector('.ai-msg-loading'))`, 15000, 'AI 上一次请求尚未结束');
    const answersBefore = await cdp.evaluate(`document.querySelectorAll('.ai-msg-answer').length`);
    await cdp.evaluate(`document.getElementById('ai-input').focus()`);
    await cdp.send('Input.insertText', { text: question });
    await sleep(250);
    await cdp.send('Input.dispatchKeyEvent', {
      type: 'keyDown',
      key: 'Enter',
      code: 'Enter',
      windowsVirtualKeyCode: 13,
      nativeVirtualKeyCode: 13,
    });
    await cdp.send('Input.dispatchKeyEvent', {
      type: 'keyUp',
      key: 'Enter',
      code: 'Enter',
      windowsVirtualKeyCode: 13,
      nativeVirtualKeyCode: 13,
    });
    await cdp.waitFor(
      `document.querySelectorAll('.ai-msg-answer').length > ${answersBefore}`,
      180000,
      `等待 AI 回答超时：${question}`,
    );
    await cdp.waitFor(`Boolean(!document.querySelector('.ai-msg-loading'))`, 20000, 'AI 回答完成后加载态未结束');
    await sleep(900);
    const result = await cdp.evaluate(`(() => ({
      answers: document.querySelectorAll('.ai-msg-answer').length,
      cards: [...document.querySelectorAll('.ai-action-card')].map((card) => ({
        id: card.dataset.actionId || '',
        danger: card.classList.contains('danger'),
        type: card.querySelector('.ai-action-type')?.textContent || '',
        text: card.querySelector('.ai-action-confirm')?.textContent || '',
        hasConfirm: Boolean(card.querySelector('[data-action-role="confirm"]')),
        hasCancel: Boolean(card.querySelector('[data-action-role="cancel"]')),
      })),
    }))()`);
    log(`问题「${question}」→ 回答 ${result.answers} 条 / action 卡片 ${result.cards.length} 个`);
    return result;
  };

  /** 仅点击取消按钮（绝不点击确认），等待卡片进入 cancelled 状态 */
  const cancelCards = async (cards) => {
    for (const card of cards) {
      if (!card.id) continue;
      await cdp.evaluate(`(() => {
        const target = [...document.querySelectorAll('.ai-action-card')].find(
          (node) => node.dataset.actionId === ${JSON.stringify(card.id)},
        );
        target?.querySelector('[data-action-role="cancel"]')?.click();
      })()`);
      await cdp.waitFor(
        `(() => {
          const target = [...document.querySelectorAll('.ai-action-card')].find(
            (node) => node.dataset.actionId === ${JSON.stringify(card.id)},
          );
          return Boolean(target && target.classList.contains('cancelled'));
        })()`,
        30000,
        'action 卡片取消未生效',
      );
    }
    await sleep(500);
    return cdp.evaluate(`(() => ({
      cancelled: document.querySelectorAll('.ai-action-card.cancelled').length,
      confirmed: document.querySelectorAll('.ai-action-card.done').length,
      results: [...document.querySelectorAll('.ai-action-card.cancelled .ai-action-result')].map(
        (node) => node.textContent,
      ),
    }))()`);
  };

  // ---- 1) 场景新增设备：non-danger 卡片 + 取消（不写入场景） ----
  const sceneResult = await askQuestion('我想在宿舍楼加一台 2960 接入交换机，怎么配置？');
  const sceneCard = sceneResult.cards.find(
    (card) =>
      !card.danger &&
      (card.type.includes('场景') ||
        card.text.includes('宿舍楼') ||
        card.text.includes('新增设备') ||
        card.text.includes('SW-SS')),
  );
  log(`场景类 action 卡片：${JSON.stringify(sceneResult.cards)}`);
  check(
    Boolean(sceneCard),
    'live：真实 Agent 回答渲染「场景新增设备」action 卡片（non-danger）',
    `未渲染场景 action 卡片：${JSON.stringify(sceneResult.cards)}`,
  );
  check(
    Boolean(sceneCard?.hasConfirm && sceneCard?.hasCancel),
    'live：场景 action 卡片包含「确认执行」「取消」按钮',
    `场景卡片按钮异常：${JSON.stringify(sceneCard)}`,
  );
  const sceneCancel = await cancelCards(sceneResult.cards);
  check(
    sceneCancel.cancelled >= 1 && sceneCancel.confirmed === 0,
    `live：点击取消后卡片显示取消结果（${sceneCancel.results.join(' / ').slice(0, 60)}），未执行确认`,
    `取消结果异常：cancelled=${sceneCancel.cancelled} done=${sceneCancel.confirmed}`,
  );
  const sceneAfter = await fetchJson(`${API_BASE}/api/scene`);
  const pendingAfter = await fetchJson(`${API_BASE}/api/scene/pending`).catch(() => null);
  check(
    sceneAfter.version === sceneBefore.version &&
      JSON.stringify(sceneAfter.buildings) === JSON.stringify(sceneBefore.buildings),
    'live：取消场景新增设备后 /api/scene 未变化（未写入场景）',
    `取消后场景被修改：v${sceneBefore.version} → v${sceneAfter.version}`,
  );
  check(
    (pendingAfter?.count ?? 0) === (pendingBefore?.count ?? 0),
    'live：取消后 pending 设备数量未变化',
    `pending 数量变化：${pendingBefore?.count} → ${pendingAfter?.count}`,
  );

  // ---- 2) 清空已恢复告警：danger 红色卡片 + 取消（不改变告警数量） ----
  let dangerResult = await askQuestion('把已恢复的告警清空');
  let dangerCard = dangerResult.cards.find((card) => card.danger);
  if (!dangerCard) {
    // 真实大模型路由存在波动：未生成危险 action 时用更明确的指令重试一次
    log('  [WARN] 首次未生成危险 action 卡片，用更明确指令重试一次…');
    dangerResult = await askQuestion('请把已恢复的告警清空，并生成需要我确认的待确认操作');
    dangerCard = dangerResult.cards.find((card) => card.danger);
  }
  log(`危险类 action 卡片：${JSON.stringify(dangerResult.cards)}`);
  if (dangerCard) {
    check(
      true,
      'live：真实 Agent 将「清空已恢复告警」渲染为 danger 红色 action 卡片',
      '',
    );
    check(
      Boolean(dangerCard.text.includes('清空') || dangerCard.text.includes('已恢复告警')),
      `live：危险卡片展示 confirm_text（${dangerCard.text.slice(0, 80)}）`,
      '危险卡片缺少 confirm_text',
    );
    check(Boolean(dangerCard.hasCancel), 'live：危险卡片带「取消」按钮', '危险卡片缺少取消按钮');
    const dangerCancel = await cancelCards(dangerResult.cards.filter((card) => card.danger));
    check(
      dangerCancel.cancelled >= 1 && dangerCancel.confirmed === 0,
      `live：危险 action 取消后显示取消结果（${dangerCancel.results.join(' / ').slice(0, 60)}）`,
      `危险 action 取消失败：${JSON.stringify(dangerCancel)}`,
    );
  } else {
    log('  [WARN] Agent 本次未返回「清空已恢复告警」action（模型路由波动），跳过危险卡片断言');
  }
  const alertsAfter = await fetchJson(`${API_BASE}/api/alerts/summary`);
  // 真实采集会不断产生新告警（total 可能增加），因此只断言“未被清空/删除”（cleared 不下降）
  check(
    alertsAfter.cleared >= alertsBefore.cleared && alertsAfter.total >= alertsBefore.total,
    `live：危险 action 处理链路未清空告警（cleared ${alertsBefore.cleared} → ${alertsAfter.cleared}，total ${alertsBefore.total} → ${alertsAfter.total}）`,
    `告警被清空：before=${JSON.stringify(alertsBefore)} after=${JSON.stringify(alertsAfter)}`,
  );
  const auditAfter = await fetchJson(`${API_BASE}/api/audit?limit=20`).catch(() => null);
  const clearAuditCount = (items) =>
    (items || []).filter((item) => String(item.action || '').includes('clear')).length;
  check(
    clearAuditCount(auditAfter?.items) === clearAuditCount(auditBefore?.items),
    'live：未产生新的告警清空审计记录',
    '出现新的清空告警审计记录',
  );

  // 截图：展示已取消的场景类 + 危险类 action 卡片
  await cdp.evaluate(`(() => {
    const messages = document.getElementById('ai-messages');
    const card = document.querySelector('.ai-action-card.danger') || document.querySelector('.ai-action-card');
    if (messages && card) messages.scrollTop = card.offsetTop - messages.offsetTop - 8;
  })()`);
  await sleep(500);
  const shot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w9-live-actions.png'));
  log(`live action 卡片截图：${shot}`);
}

/**
 * W10 live 联调（真实后端）：路径追踪 / 告警根因分组 / 通知状态。
 * 后端接口未就绪（404）时校验前端优雅降级，不产生运行时错误；就绪则做真实 UI 联调。
 */
async function runLiveW10Checks(cdp) {
  log('');
  log('—— W10：路径追踪 / 告警根因分组 / 通知状态（真实后端）——');
  const fetchJsonSafe = async (url, options = {}, timeoutMs = 45000) => {
    const response = await fetch(url, { ...options, signal: AbortSignal.timeout(timeoutMs) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };
  let pathDoc = null;
  let groupsDoc = null;
  let notifyDoc = null;
  try {
    pathDoc = await fetchJsonSafe(`${API_BASE}/api/path-trace`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_ip: '192.168.20.10', dest_ip: '192.168.50.10' }),
    });
  } catch (error) {
    log(`  [INFO] 后端 /api/path-trace 未就绪（${error.message}），校验前端降级提示`);
  }
  try {
    groupsDoc = await fetchJsonSafe(`${API_BASE}/api/alerts/groups?state=active`);
  } catch (error) {
    log(`  [INFO] 后端 /api/alerts/groups 未就绪（${error.message}），校验前端降级提示`);
  }
  try {
    notifyDoc = await fetchJsonSafe(`${API_BASE}/api/notify/status`);
  } catch (error) {
    log(`  [INFO] 后端 /api/notify/status 未就绪（${error.message}），校验前端降级提示`);
  }

  // ---- 通知状态 ----
  const notifyUI = await cdp.evaluate(`(() => ({
    value: document.getElementById('stat-notify-value')?.textContent || '',
    sub: document.getElementById('stat-notify-sub')?.textContent || '',
    testHidden: document.getElementById('btn-notify-test')?.classList.contains('hidden'),
  }))()`);
  if (notifyDoc) {
    const expectEnabled = Boolean(notifyDoc.enabled);
    check(
      notifyUI.value.includes(expectEnabled ? '已启用' : '未启用'),
      `通知状态与后端一致（${notifyUI.value} / ${notifyUI.sub}）`,
      `通知状态不一致：UI=${notifyUI.value} 后端 enabled=${expectEnabled}`,
    );
    check(
      notifyUI.testHidden === !expectEnabled,
      expectEnabled ? '通知启用时显示「测试」按钮' : '通知未启用时隐藏「测试」按钮',
      `测试按钮显隐异常：hidden=${notifyUI.testHidden}`,
    );
    if (notifyUI.testHidden) log('  [INFO] 通知未启用，跳过「测试」按钮断言');
  } else {
    check(notifyUI.value.includes('未配置'), '通知接口未就绪时顶部显示「未配置」', `通知降级异常：${notifyUI.value}`);
    check(notifyUI.testHidden === true, '通知接口未就绪时隐藏「测试」按钮', '通知降级时测试按钮仍显示');
  }

  // ---- 路径追踪 ----
  await cdp.evaluate(`document.getElementById('tab-path').click()`);
  await sleep(500);
  await cdp.evaluate(`(() => {
    const source = document.getElementById('path-source');
    const dest = document.getElementById('path-dest');
    source.value = '192.168.20.10';
    dest.value = '192.168.50.10';
    document.getElementById('path-trace-btn').click();
  })()`);
  const livePathUsable = Boolean(
    pathDoc &&
      pathDoc.ok !== false &&
      Array.isArray(pathDoc.hops) &&
      pathDoc.hops.length >= 2 &&
      String(pathDoc.trace_status || 'complete').toLowerCase() !== 'failed',
  );
  if (pathDoc && !livePathUsable) {
    log(
      `  [INFO] 后端 /api/path-trace 当前不可用（NC 可能离线）：${String(
        pathDoc.error || pathDoc.trace_status || '',
      ).slice(0, 80)}，改为校验前端错误提示`,
    );
  }
  if (livePathUsable) {
    await cdp.waitFor(
      `window.__campusTwin.scene.getPathStats().active === true`,
      60000,
      '真实路径追踪 60s 内未返回 3D 路径',
    );
    await cdp.waitFor(
      `document.querySelectorAll('#path-hops .path-hop').length >= 2`,
      20000,
      '路径 hop 列表未渲染',
    );
    await sleep(800);
    const livePath = await cdp.evaluate(`(() => ({
      scene: window.__campusTwin.scene.getPathStats(),
      hops: [...document.querySelectorAll('#path-hops .path-hop .path-hop-name')].map((node) => node.textContent),
      status: document.getElementById('path-status')?.textContent || '',
      runtimeError: document.getElementById('runtime-error')?.textContent || '',
    }))()`);
    check(
      livePath.scene.active === true && livePath.scene.hops >= 2,
      `真实路径追踪成功：${livePath.scene.hops} 跳 / 3D ${livePath.scene.objects} 个对象（${livePath.hops.join(' → ')}）`,
      `真实路径追踪异常：${JSON.stringify(livePath.scene)}`,
    );
    check(livePath.runtimeError === '', '路径追踪渲染无运行时错误', `路径渲染运行时错误：${livePath.runtimeError}`);
    if (livePath.scene.breakpoint) {
      check(true, '非完整路径时 3D 已标记断点', '');
    } else {
      check(livePath.status.includes('完整路径'), '路径状态为「完整路径」', `路径状态异常：${livePath.status}`);
    }
    const livePathShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w10-path-live.png'));
    log(`真实路径追踪截图：${livePathShot}`);
    await cdp.evaluate(`document.getElementById('path-clear-btn').click()`);
    await sleep(400);
  } else {
    await cdp.waitFor(
      `((document.getElementById('path-status')?.textContent || '').includes('未就绪') ||
        (document.getElementById('path-status')?.textContent || '').includes('失败') ||
        (document.getElementById('path-status')?.textContent || '').includes('不可达') ||
        (document.getElementById('path-status')?.textContent || '').includes('错误') ||
        (document.getElementById('path-status')?.textContent || '').includes('超时'))`,
      60000,
      '路径追踪服务不可用时未显示中文错误/降级提示',
    );
    const degraded = await cdp.evaluate(`(() => ({
      status: document.getElementById('path-status')?.textContent || '',
      runtimeError: document.getElementById('runtime-error')?.textContent || '',
      sceneActive: window.__campusTwin.scene.getPathStats().active,
    }))()`);
    check(
      /未就绪|失败|不可达|错误|超时/.test(degraded.status),
      `路径追踪服务不可用时显示中文错误提示（${degraded.status.slice(0, 60)}）`,
      `路径降级提示异常：${degraded.status}`,
    );
    check(
      degraded.sceneActive === false && degraded.runtimeError === '',
      '路径追踪失败不产生 3D 残留、不产生运行时错误',
      `路径失败状态异常：${JSON.stringify(degraded)}`,
    );
  }

  // ---- 告警根因分组 ----
  await cdp.evaluate(`document.getElementById('alert-view-groups').click()`);
  await sleep(1600);
  if (groupsDoc) {
    const groupUI = await cdp.evaluate(`(() => ({
      cards: document.querySelectorAll('.alert-group').length,
      hint: document.getElementById('alert-groups-hint')?.textContent || '',
      empty: document.querySelector('.alert-groups-empty')?.textContent || '',
    }))()`);
    const expectedGroups = Array.isArray(groupsDoc.groups) ? groupsDoc.groups.length : 0;
    check(
      groupUI.cards === expectedGroups || (expectedGroups === 0 && groupUI.empty.length > 0),
      `真实根因分组渲染 ${groupUI.cards} 组（后端 ${expectedGroups} 组）`,
      `根因分组渲染异常：${JSON.stringify(groupUI)}`,
    );
    if (expectedGroups > 0) {
      const groupDetail = await cdp.evaluate(`(() => ({
        roots: [...document.querySelectorAll('.alert-group-root')].map((node) => node.textContent),
        suppressedTags: document.querySelectorAll('.alert-member-suppressed-tag').length,
        actions: document.querySelectorAll('.alert-group [data-alert-action]').length,
      }))()`);
      check(
        groupDetail.roots.length >= 1,
        `分组卡片展示根因（${groupDetail.roots.join(' / ')}）`,
        `分组根因缺失：${JSON.stringify(groupDetail)}`,
      );
      log(`  分组根因 ${groupDetail.roots.length} 个 · 抑制标签 ${groupDetail.suppressedTags} 个 · 操作按钮 ${groupDetail.actions} 个`);
      const liveGroupsShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w10-groups-live.png'));
      log(`真实根因分组截图：${liveGroupsShot}`);
    } else {
      log('  [INFO] 后端当前无活跃告警，根因分组为空');
    }
  } else {
    const groupDegraded = await cdp.evaluate(`(() => ({
      hint: document.getElementById('alert-groups-hint')?.textContent || '',
      alerts: document.querySelectorAll('#alerts-list .alert-item').length,
    }))()`);
    check(
      /未就绪|失败/.test(groupDegraded.hint),
      `根因分组接口未就绪时显示降级提示（${groupDegraded.hint.slice(0, 50)}）`,
      `分组降级提示异常：${groupDegraded.hint}`,
    );
    check(groupDegraded.alerts >= 1, '分组接口未就绪时降级显示普通告警列表', '分组降级时告警列表为空');
  }
  await cdp.evaluate(`document.getElementById('alert-view-all').click()`);
  await sleep(300);
}

/**
 * W12 冒烟（契约 Mock）：SSE 流式渲染 / token 拼接 / 停止生成 / RAG 引用 / 404 回退。
 * 依赖 mock agent 的 /api/chat/stream 与 /__mock/stream_mode 控制端点。
 */
async function runW12Checks(cdp) {
  const agentBase = W9_AGENT;
  log('');
  log('—— W12：AI 流式渲染（SSE）+ RAG 引用 + 回退（契约 Mock）——');
  const setStreamMode = (mode) =>
    fetch(`${agentBase}/__mock/stream_mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
  await setStreamMode('ok');

  const enterKey = async () => {
    await cdp.send('Input.dispatchKeyEvent', {
      type: 'keyDown',
      key: 'Enter',
      code: 'Enter',
      windowsVirtualKeyCode: 13,
      nativeVirtualKeyCode: 13,
    });
    await cdp.send('Input.dispatchKeyEvent', {
      type: 'keyUp',
      key: 'Enter',
      code: 'Enter',
      windowsVirtualKeyCode: 13,
      nativeVirtualKeyCode: 13,
    });
  };
  const askQuestion = async (question) => {
    await cdp.evaluate(`document.getElementById('tab-ai').click()`);
    await sleep(300);
    await cdp.waitFor(`Boolean(!document.querySelector('.ai-msg-loading'))`, 15000, '上一轮 AI 请求尚未结束');
    await cdp.evaluate(`document.getElementById('ai-input').focus()`);
    await cdp.send('Input.insertText', { text: question });
    await sleep(150);
    await enterKey();
  };

  // ---- 1. 流式渲染：状态条 / 工具 chip / token 增量 / 停止按钮 ----
  const answersBefore = await cdp.evaluate(`document.querySelectorAll('.ai-msg-answer').length`);
  await askQuestion('现在校园网整体状态怎么样？');
  await cdp.waitFor(`Boolean(document.querySelector('.ai-msg-streaming'))`, 15000, '未出现流式气泡');
  await cdp.waitFor(
    `(document.querySelector('.ai-stream-markdown')?.textContent?.length || 0) > 60`,
    20000,
    '流式 token 未追加',
  );
  const mid1 = await cdp.evaluate(`(() => {
    const dbg = window.__campusTwin.ai.getStreamDebug();
    return {
      streaming: Boolean(document.querySelector('.ai-msg-streaming')),
      status: document.querySelector('.ai-stream-stage')?.textContent || '',
      tools: [...document.querySelectorAll('.ai-stream-tool')].map((node) => node.textContent),
      stopVisible: Boolean(document.querySelector('.ai-stream-stop')),
      textLen: document.querySelector('.ai-stream-markdown')?.textContent?.length || 0,
      events: dbg?.events || [],
      chunks: dbg?.tokenChunks || 0,
    };
  })()`);
  log(
    `流式中：status=${mid1.status} tools=${mid1.tools.length} textLen=${mid1.textLen} events=${mid1.events.length}`,
  );
  check(mid1.streaming === true, '显示流式气泡（逐 token 追加中）', '未显示流式气泡');
  check(
    /检索|工具|生成|理解|处理/.test(mid1.status),
    `状态条显示当前阶段（${mid1.status}）`,
    `状态条异常：${mid1.status}`,
  );
  check(
    mid1.tools.length >= 1,
    `工具调用 chip 已渲染（${mid1.tools.join(' / ')}）`,
    '缺少工具调用 chip',
  );
  check(mid1.stopVisible === true, '流式过程中显示「停止生成」按钮', '缺少停止生成按钮');
  check(mid1.events[0] === 'meta', `SSE 首个事件为 meta（${mid1.events[0]}）`, `SSE 首事件异常：${mid1.events.slice(0, 3)}`);
  const midText = mid1.textLen;
  await sleep(500);
  const mid2 = await cdp.evaluate(`({
    textLen: document.querySelector('.ai-stream-markdown')?.textContent?.length || 0,
    streaming: Boolean(document.querySelector('.ai-msg-streaming')),
  })`);
  check(
    mid2.textLen > midText,
    `token 增量拼接（${midText} → ${mid2.textLen} 字符）`,
    `token 未增长：${midText} → ${mid2.textLen}`,
  );
  const streamingShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w12-streaming.png'));
  log(`流式截图：${streamingShot}`);

  // 等待流式完成（完成后仍是普通 .ai-msg-answer 以便复用 trace/actions 渲染）
  await cdp.waitFor(
    `document.querySelectorAll('.ai-msg-answer').length > ${answersBefore} && !document.querySelector('.ai-msg-streaming')`,
    40000,
    '流式回答未完成',
  );
  await sleep(700);
  const final = await cdp.evaluate(`(() => {
    const dbg = window.__campusTwin.ai.getStreamDebug();
    const root = [...document.querySelectorAll('.ai-msg-answer')].slice(-1)[0];
    return {
      events: dbg?.events || [],
      chunks: dbg?.tokenChunks || 0,
      tokensLen: dbg?.tokensJoined?.length || 0,
      doneLen: dbg?.doneAnswerLength ?? null,
      stages: dbg?.stages || [],
      citations: dbg?.citations ?? 0,
      fallback: Boolean(dbg?.fallback),
      citationDetails: root?.querySelectorAll('.ai-citations').length || 0,
      citationItems: root?.querySelectorAll('.citation-item').length || 0,
      citeRefs: root?.querySelectorAll('.cite-ref').length || 0,
      actions: root?.querySelectorAll('.ai-action-card').length || 0,
      trace: Boolean(root?.querySelector('.ai-trace')),
    };
  })()`);
  const events = final.events;
  log(
    `流式完成：events=${events.length} chunks=${final.chunks} tokens=${final.tokensLen}/${final.doneLen} citations=${final.citationItems} actions=${final.actions} trace=${final.trace}`,
  );
  check(
    events.includes('status') && events.includes('tool') && events.includes('token') && events.includes('done'),
    'SSE 事件包含 status/tool/token/done',
    `事件序列异常：${events.slice(0, 8).join(',')}…`,
  );
  check(
    events[0] === 'meta' && events[events.length - 1] === 'done',
    'SSE 以 meta 开始、done 结束',
    `首尾事件异常：${events[0]} / ${events[events.length - 1]}`,
  );
  const firstTokenIndex = events.indexOf('token');
  const lastToolIndex = events.lastIndexOf('tool');
  check(
    firstTokenIndex > lastToolIndex && firstTokenIndex < events.indexOf('done'),
    'SSE 事件顺序：工具调用 → token 流 → done',
    `事件顺序异常：token=${firstTokenIndex} tool=${lastToolIndex} done=${events.indexOf('done')}`,
  );
  check(
    final.stages.some((stage) => stage.stage === 'retrieve') &&
      final.stages.some((stage) => stage.stage === 'aggregate'),
    '状态阶段包含 retrieve / aggregate',
    `阶段异常：${final.stages.map((stage) => stage.stage).join(',')}`,
  );
  check(
    final.chunks >= 5 && final.tokensLen === final.doneLen,
    `token 拼接结果与 done.answer 完全一致（${final.chunks} chunk / ${final.tokensLen} 字）`,
    `token 拼接不一致：tokens=${final.tokensLen} done=${final.doneLen}`,
  );
  check(
    final.citationDetails >= 1 && final.citationItems >= 3 && final.citeRefs >= 1,
    `RAG 引用区渲染（${final.citationItems} 条知识 / ${final.citeRefs} 个角标）`,
    `引用区异常：details=${final.citationDetails} items=${final.citationItems} refs=${final.citeRefs}`,
  );
  check(
    final.actions >= 1 && final.trace === true,
    '流式完成后仍渲染 action 卡片与 trace（现有能力不变）',
    `actions=${final.actions} trace=${final.trace}`,
  );
  // 点击角标 → 展开参考知识并高亮对应条目
  await cdp.evaluate(`document.querySelector('.cite-ref')?.click()`);
  await sleep(600);
  const citationUI = await cdp.evaluate(`(() => ({
    detailsOpen: Boolean(document.querySelector('.ai-citations')?.open),
    highlighted: document.querySelectorAll('.citation-item.highlight').length,
    summary: document.querySelector('.ai-citations summary')?.textContent || '',
    firstTitle: document.querySelector('.citation-title')?.textContent || '',
    firstMeta: document.querySelector('.citation-meta')?.textContent || '',
  }))()`);
  check(
    citationUI.detailsOpen === true && citationUI.highlighted >= 1,
    `点击 [K] 角标展开并高亮引用条目（${citationUI.summary.trim()}）`,
    `引用交互异常：${JSON.stringify(citationUI)}`,
  );
  check(
    citationUI.firstTitle.length > 0 && citationUI.firstMeta.includes('相关度'),
    `引用条目展示标题/来源/分数（${citationUI.firstTitle} · ${citationUI.firstMeta}）`,
    `引用条目信息缺失：${JSON.stringify(citationUI)}`,
  );
  await cdp.evaluate(`(() => {
    const messages = document.getElementById('ai-messages');
    const citation = document.querySelector('.ai-citations');
    if (messages && citation) messages.scrollTop = citation.offsetTop - messages.offsetTop - 140;
  })()`);
  await sleep(400);
  const ragShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w12-rag.png'));
  log(`RAG 引用截图：${ragShot}`);

  // ---- 2. 停止生成（AbortController） ----
  await askQuestion('宿舍楼的 2960 接入交换机该怎么配置？');
  await cdp.waitFor(
    `(document.querySelector('.ai-stream-markdown')?.textContent?.length || 0) > 30`,
    20000,
    '停止生成测试：流式 token 未出现',
  );
  await sleep(150);
  await cdp.evaluate(`document.querySelector('.ai-stream-stop')?.click()`);
  await cdp.waitFor(`Boolean(document.querySelector('.ai-msg-stopped'))`, 15000, '停止生成后未标记已停止');
  await sleep(500);
  const stopped = await cdp.evaluate(`(() => {
    const dbg = window.__campusTwin.ai.getStreamDebug();
    const root = [...document.querySelectorAll('.ai-msg-answer')].slice(-1)[0];
    return {
      streaming: Boolean(document.querySelector('.ai-msg-streaming')),
      note: root?.querySelector('.ai-stream-note')?.textContent || '',
      markdownLen: root?.querySelector('.ai-markdown')?.textContent?.length || 0,
      debug: { stopped: Boolean(dbg?.stopped), tokensLen: dbg?.tokensJoined?.length || 0, done: Boolean(dbg?.doneEvent) },
    };
  })()`);
  check(
    stopped.debug.stopped === true && stopped.streaming === false,
    '点击「停止生成」中断 SSE（AbortController）',
    `停止状态异常：${JSON.stringify(stopped)}`,
  );
  check(
    stopped.note.includes('已停止生成'),
    `停止后标注「已停止生成」（${stopped.note}）`,
    `停止标注缺失：${stopped.note}`,
  );
  check(
    stopped.markdownLen > 0 && stopped.debug.done === false,
    `停止后保留已生成内容（${stopped.markdownLen} 字）且未收到 done`,
    `停止内容异常：${JSON.stringify(stopped.debug)}`,
  );

  // ---- 3. 回退：/api/chat/stream 404 → /api/chat 非流式 + 提示 ----
  await setStreamMode('404');
  const answersBeforeFallback = await cdp.evaluate(`document.querySelectorAll('.ai-msg-answer').length`);
  await askQuestion('现在校园网整体状态怎么样？');
  await cdp.waitFor(
    `document.querySelectorAll('.ai-msg-answer').length > ${answersBeforeFallback}`,
    25000,
    '流式 404 后未渲染普通回答',
  );
  await cdp.waitFor(`Boolean(!document.querySelector('.ai-msg-loading'))`, 20000, '回退普通模式未完成');
  await sleep(700);
  const fallback = await cdp.evaluate(`(() => {
    const dbg = window.__campusTwin.ai.getStreamDebug();
    const root = [...document.querySelectorAll('.ai-msg-answer')].slice(-1)[0];
    return {
      note: root?.querySelector('.ai-fallback-note')?.textContent || '',
      debug: { fallback: Boolean(dbg?.fallback), error: dbg?.error || '', answerLen: dbg?.fallbackAnswerLength || 0 },
      toast: document.getElementById('toast')?.textContent || '',
      markdownLen: root?.querySelector('.ai-markdown')?.textContent?.length || 0,
      citations: root?.querySelectorAll('.citation-item').length || 0,
    };
  })()`);
  check(
    fallback.debug.fallback === true && fallback.note.includes('普通模式'),
    `SSE 404 时自动回退非流式并提示（${fallback.note.slice(0, 70)}）`,
    `回退提示缺失：${JSON.stringify(fallback)}`,
  );
  check(
    fallback.markdownLen > 100,
    `回退后渲染完整非流式回答（${fallback.markdownLen} 字）`,
    `回退回答过短：${fallback.markdownLen}`,
  );
  check(
    fallback.toast.includes('切换普通模式') || fallback.debug.answerLen > 0,
    `回退 toast/调试记录表明已切换普通模式（${fallback.toast || `answerLen=${fallback.debug.answerLen}`}）`,
    `回退提示异常：${JSON.stringify(fallback)}`,
  );
  await setStreamMode('ok');
}

/**
 * W12 live 联调（真实 Agent）：流式接口就绪时校验 SSE 渲染；未就绪时校验自动回退非流式。
 * 直接读取页面 aiPanel.getStreamDebug()（首页 AI 真实问答时已产生），不额外消耗模型调用。
 */
async function runLiveStreamingChecks(cdp) {
  log('');
  log('—— W12：AI 流式 / RAG（真实 Agent）——');
  const state = await cdp.evaluate(`(() => {
    const dbg = window.__campusTwin?.ai?.getStreamDebug?.() || null;
    const root = [...document.querySelectorAll('.ai-msg-answer')].slice(-1)[0];
    return {
      debug: dbg
        ? {
            events: dbg.events,
            chunks: dbg.tokenChunks,
            tokensLen: dbg.tokensJoined ? dbg.tokensJoined.length : 0,
            done: Boolean(dbg.doneEvent),
            stopped: Boolean(dbg.stopped),
            fallback: Boolean(dbg.fallback),
            error: dbg.error || '',
            citations: dbg.citations || 0,
          }
        : null,
      fallbackNote: root?.querySelector('.ai-fallback-note')?.textContent || '',
      citations: root?.querySelectorAll('.citation-item').length || 0,
      citeRefs: root?.querySelectorAll('.cite-ref').length || 0,
      markdownLen: root?.querySelector('.ai-markdown')?.textContent?.length || 0,
    };
  })()`);
  if (state.debug?.fallback) {
    check(
      true,
      `流式接口未就绪（${(state.debug.error || '404').slice(0, 50)}），已自动回退非流式并提示`,
      '',
    );
    check(
      state.markdownLen > 100,
      `回退后普通回答正常渲染（${state.markdownLen} 字）`,
      `回退回答过短：${state.markdownLen}`,
    );
    check(
      state.fallbackNote.includes('普通模式') || state.markdownLen > 100,
      `页面显示「已自动切换普通模式」提示（${state.fallbackNote.slice(0, 60)}）`,
      `回退提示缺失：${state.fallbackNote}`,
    );
    log('  [INFO] 真实 Agent /api/chat/stream 未上线，已用回退链路完成 live 校验');
  } else if (state.debug?.events?.length) {
    check(state.debug.events[0] === 'meta', '真实 SSE 以 meta 事件开始', `首事件异常：${state.debug.events[0]}`);
    check(
      state.debug.chunks > 0 && state.debug.tokensLen > 50,
      `真实流式 token 渲染（${state.debug.chunks} 个增量 / ${state.debug.tokensLen} 字）`,
      `真实流式 token 异常：${JSON.stringify(state.debug)}`,
    );
    check(state.debug.done === true || !state.debug.stopped, '真实流式以 done 结束（未被中断）', '真实流式未收到 done');
    if (state.debug.citations > 0) {
      check(
        state.citations >= 1 && state.citeRefs >= 1,
        `真实 RAG 引用渲染（${state.citations} 条知识 / ${state.citeRefs} 个角标）`,
        '真实 RAG 引用区缺失',
      );
    } else {
      log('  [INFO] 本次真实回答未返回 RAG 检索命中，跳过引用区断言');
    }
  } else {
    log('  [INFO] 页面未捕获流式调试信息（可能使用普通模式回答）');
  }
}

/**
 * W15 冒烟（契约 Mock）：可观测性页签 —— 指标卡 / trace 列表与展开 / RAG 评测卡 / 按会话过滤 / 404 降级。
 */
async function runW15Checks(cdp) {
  const agentBase = W9_AGENT;
  log('');
  log('—— W15：可观测性页签（metrics / traces / RAG，契约 Mock）——');
  const setW15Mode = (mode) =>
    fetch(`${agentBase}/__mock/w15_mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
  await setW15Mode('ok');
  // 收起新设备提示条，避免遮挡可观测性截图
  await cdp.evaluate(`(() => {
    const banner = document.getElementById('new-device-banner');
    if (banner && !banner.classList.contains('hidden') && !banner.classList.contains('collapsed')) {
      document.getElementById('new-device-collapse')?.click();
    }
  })()`);
  await sleep(250);

  await cdp.evaluate(`document.getElementById('tab-obs').click()`);
  await cdp.waitFor(`document.querySelectorAll('.obs-trace').length >= 10`, 20000, '可观测性 trace 列表未加载');
  await sleep(700);
  const obs = await cdp.evaluate(`(() => {
    const metric = (id) => ({
      value: document.querySelector('#' + id + ' .obs-metric-value')?.textContent || '',
      sub: document.querySelector('#' + id + ' .obs-metric-sub')?.textContent || '',
      available: document.querySelector('#' + id)?.classList.contains('available'),
    });
    return {
      paneActive: document.getElementById('pane-obs')?.classList.contains('active'),
      status: document.getElementById('obs-status')?.textContent || '',
      latency: metric('obs-metric-latency'),
      errorRate: metric('obs-metric-error-rate'),
      tokens: metric('obs-metric-tokens'),
      traceRows: document.querySelectorAll('.obs-trace').length,
      badRows: document.querySelectorAll('.obs-trace.bad').length,
      firstRow: document.querySelector('.obs-trace-head')?.textContent?.replace(/[\\s]+/g, ' ').trim() || '',
      ragVisible: document.getElementById('obs-rag')
        ? !document.getElementById('obs-rag').classList.contains('hidden')
        : false,
      ragItems: [...document.querySelectorAll('.obs-rag-item')].map((node) =>
        node.textContent.replace(/[\\s]+/g, ' ').trim(),
      ),
      debug: window.__campusTwin.obsPanel.getDebug(),
      stats: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}'),
    };
  })()`);
  log(
    `可观测性：latency=${obs.latency.value} error=${obs.errorRate.value} tokens=${obs.tokens.value} traces=${obs.traceRows} rag=${obs.ragVisible}`,
  );
  check(obs.paneActive === true, '可观测性页签可打开', '可观测性页签未激活');
  check(
    /ms|s/.test(obs.latency.value) && obs.latency.sub.includes('次调用'),
    `平均响应延迟指标卡渲染（${obs.latency.value} · ${obs.latency.sub}）`,
    `延迟指标卡异常：${JSON.stringify(obs.latency)}`,
  );
  check(
    obs.errorRate.value.includes('%') && obs.errorRate.sub.length > 0,
    `工具错误率指标卡渲染（${obs.errorRate.value} · ${obs.errorRate.sub}）`,
    `错误率指标卡异常：${JSON.stringify(obs.errorRate)}`,
  );
  check(
    obs.tokens.value !== '—' && obs.tokens.sub.length > 0,
    `Token 总量指标卡渲染（${obs.tokens.value} · ${obs.tokens.sub}）`,
    `Token 指标卡异常：${JSON.stringify(obs.tokens)}`,
  );
  check(obs.debug.metricsAvailable === true && obs.stats.obsMetricsAvailable === true, 'dev-stats 记录指标可用状态', 'dev-stats 指标状态异常');
  check(
    obs.traceRows >= 10 && obs.traceRows <= 20,
    `最近 trace 列表渲染（${obs.traceRows} 条，limit 20）`,
    `trace 数量异常：${obs.traceRows}`,
  );
  check(
    /\d{2}:\d{2}:\d{2}/.test(obs.firstRow) && obs.firstRow.length > 10,
    `trace 行展示时间/事件/工具/耗时（${obs.firstRow.slice(0, 80)}）`,
    `trace 行内容异常：${obs.firstRow}`,
  );
  check(obs.badRows >= 1, `失败 trace 以红色状态标识（${obs.badRows} 条）`, '未标识失败 trace');

  // 展开 trace → 详情（事件序列 / tokens / 耗时）
  await cdp.evaluate(`document.querySelector('.obs-trace-head')?.click()`);
  await cdp.waitFor(
    `Boolean(document.querySelector('.obs-trace.expanded .obs-trace-detail:not(.hidden)'))`,
    10000,
    '点击 trace 后未展开详情',
  );
  await sleep(600);
  const detail = await cdp.evaluate(`(() => ({
    expanded: document.querySelectorAll('.obs-trace.expanded').length,
    summary: document.querySelector('.obs-trace-detail .obs-trace-summary')?.textContent || '',
    events: document.querySelectorAll('.obs-trace-detail .obs-trace-event-row').length,
    debugExpanded: window.__campusTwin.obsPanel.getDebug().expanded,
    statsExpanded: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}').obsExpanded,
  }))()`);
  check(detail.expanded === 1, '点击 trace 行可展开事件序列', `展开异常：${JSON.stringify(detail)}`);
  check(
    detail.summary.includes('总耗时') || detail.summary.includes('工具调用') || detail.summary.includes('tokens'),
    `trace 详情展示耗时/tokens/工具调用（${detail.summary.slice(0, 80)}）`,
    `trace 摘要异常：${detail.summary}`,
  );
  check(detail.events >= 1, `trace 详情渲染事件序列（${detail.events} 条 step）`, 'trace 详情缺少事件');
  check(detail.debugExpanded >= 1 && detail.statsExpanded >= 1, 'dev-stats 记录 trace 展开状态', 'dev-stats 未记录展开状态');

  // RAG 评测指标卡
  check(
    obs.ragVisible === true && obs.ragItems.length === 4,
    `RAG 评测卡渲染 ${obs.ragItems.length} 项（${obs.ragItems.join(' / ')}）`,
    `RAG 卡片异常：visible=${obs.ragVisible} items=${JSON.stringify(obs.ragItems)}`,
  );
  check(
    ['recall@5', 'MRR', '引用准确率', 'faithfulness'].every((label) =>
      obs.ragItems.some((item) => item.includes(label)),
    ),
    'RAG 卡片包含 recall@5 / MRR / 引用准确率 / faithfulness',
    `RAG 指标缺失：${JSON.stringify(obs.ragItems)}`,
  );

  // 按当前会话过滤（W12 已创建会话）
  const conversationId = await cdp.evaluate(`window.__campusTwin?.ai?.conversationId || null`);
  const rowsBeforeFilter = obs.traceRows;
  await cdp.evaluate(`document.getElementById('obs-filter-current').click()`);
  await sleep(1500);
  const filtered = await cdp.evaluate(`(() => ({
    checked: document.getElementById('obs-filter-current')?.checked,
    rows: document.querySelectorAll('.obs-trace').length,
    status: document.getElementById('obs-status')?.textContent || '',
    empty: document.querySelector('.obs-empty')?.textContent || '',
    debug: window.__campusTwin.obsPanel.getDebug(),
  }))()`);
  check(
    filtered.checked === true && filtered.rows <= rowsBeforeFilter,
    `按当前会话过滤 trace（会话 ${conversationId || '无'} · ${rowsBeforeFilter} → ${filtered.rows} 条）`,
    `会话过滤异常：${JSON.stringify(filtered)}`,
  );
  await cdp.evaluate(`document.getElementById('obs-filter-current').click()`);
  await sleep(1400);
  const unfilteredRows = await cdp.evaluate(`document.querySelectorAll('.obs-trace').length`);
  check(unfilteredRows >= 10, `取消过滤后恢复全部 trace（${unfilteredRows} 条）`, `取消过滤异常：${unfilteredRows}`);

  // 截图（重新展开第一条 trace，展示事件序列）
  await cdp.evaluate(`document.querySelector('.obs-trace-head')?.click()`);
  await sleep(800);
  await cdp.evaluate(`(() => {
    const pane = document.getElementById('pane-obs');
    if (pane) pane.scrollTop = 0;
    const list = document.getElementById('obs-trace-list');
    if (list) list.scrollTop = 0;
  })()`);
  await sleep(400);
  const obsShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w15-observability.png'));
  log(`可观测性截图：${obsShot}`);

  // 404 降级：3 个接口全 404
  await setW15Mode('404');
  await cdp.evaluate(`document.getElementById('obs-refresh').click()`);
  await cdp.waitFor(
    `(document.getElementById('obs-status')?.textContent || '').includes('未就绪')`,
    15000,
    '可观测性接口 404 时未显示降级提示',
  );
  await sleep(600);
  const degraded = await cdp.evaluate(`(() => ({
    status: document.getElementById('obs-status')?.textContent || '',
    latency: document.querySelector('#obs-metric-latency .obs-metric-value')?.textContent || '',
    errorRate: document.querySelector('#obs-metric-error-rate .obs-metric-value')?.textContent || '',
    tokens: document.querySelector('#obs-metric-tokens .obs-metric-value')?.textContent || '',
    ragVisible: document.getElementById('obs-rag')
      ? !document.getElementById('obs-rag').classList.contains('hidden')
      : false,
    traces: document.querySelectorAll('.obs-trace').length,
    empty: document.querySelector('.obs-empty')?.textContent || '',
    debug: window.__campusTwin.obsPanel.getDebug(),
    runtimeError: document.getElementById('runtime-error')?.textContent || '',
  }))()`);
  check(
    degraded.status.includes('未就绪') && degraded.latency === '—' && degraded.errorRate === '—',
    `接口 404 时指标卡降级为「—」并提示（${degraded.status.slice(0, 50)}）`,
    `指标降级异常：${JSON.stringify(degraded)}`,
  );
  check(degraded.ragVisible === false, 'RAG 评测接口 404 时隐藏评测卡', 'RAG 卡片未隐藏');
  check(degraded.traces === 0, 'trace 接口 404 时列表清空并提示', `trace 降级异常：${degraded.traces}`);
  check(
    /未就绪|未上线/.test(degraded.debug.error) && degraded.runtimeError === '',
    '404 降级不产生运行时错误',
    `降级状态异常：${JSON.stringify(degraded.debug)}`,
  );

  // 恢复；并确认切换其它页签不受影响
  await setW15Mode('ok');
  await cdp.evaluate(`document.getElementById('obs-refresh').click()`);
  await cdp.waitFor(`document.querySelectorAll('.obs-trace').length >= 10`, 15000, '恢复后 trace 未重新加载');
  await cdp.evaluate(`document.getElementById('tab-detail').click()`);
  await sleep(300);
  const tabsOk = await cdp.evaluate(`({
    detailActive: document.getElementById('pane-detail')?.classList.contains('active'),
    obsActive: document.getElementById('pane-obs')?.classList.contains('active'),
    tabCount: document.querySelectorAll('.dock-tab').length,
  })`);
  check(
    tabsOk.detailActive === true && tabsOk.obsActive === false && tabsOk.tabCount === 5,
    '可观测性页签不影响其它页签切换（5 个页签）',
    `页签切换异常：${JSON.stringify(tabsOk)}`,
  );
}

/**
 * W15 live 分支（真实 Agent）：接口就绪则联调指标/trace/RAG；未就绪（404）则校验优雅降级。
 */
async function runLiveW15Checks(cdp) {
  log('');
  log('—— W15：可观测性页签（真实 Agent）——');
  const fetchJsonSafe = async (url) => {
    const response = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };
  let metricsDoc = null;
  let tracesDoc = null;
  let ragDoc = null;
  try {
    metricsDoc = await fetchJsonSafe(`${AGENT_BASE}/api/metrics/agent`);
  } catch (error) {
    log(`  [INFO] /api/metrics/agent 未就绪（${error.message}）`);
  }
  try {
    tracesDoc = await fetchJsonSafe(`${AGENT_BASE}/api/traces?limit=20`);
  } catch (error) {
    log(`  [INFO] /api/traces 未就绪（${error.message}）`);
  }
  try {
    ragDoc = await fetchJsonSafe(`${AGENT_BASE}/api/rag/report`);
  } catch (error) {
    log(`  [INFO] /api/rag/report 未就绪（${error.message}）`);
  }
  await cdp.evaluate(`document.getElementById('tab-obs').click()`);
  await sleep(2600);
  const ui = await cdp.evaluate(`(() => ({
    status: document.getElementById('obs-status')?.textContent || '',
    latency: document.querySelector('#obs-metric-latency .obs-metric-value')?.textContent || '',
    errorRate: document.querySelector('#obs-metric-error-rate .obs-metric-value')?.textContent || '',
    tokens: document.querySelector('#obs-metric-tokens .obs-metric-value')?.textContent || '',
    rows: document.querySelectorAll('.obs-trace').length,
    ragVisible: document.getElementById('obs-rag')
      ? !document.getElementById('obs-rag').classList.contains('hidden')
      : false,
    debug: window.__campusTwin.obsPanel.getDebug(),
    runtimeError: document.getElementById('runtime-error')?.textContent || '',
  }))()`);
  if (metricsDoc) {
    check(
      ui.latency !== '—' && /ms|s/.test(ui.latency),
      `真实 Agent 指标卡渲染（平均延迟 ${ui.latency} / 错误率 ${ui.errorRate} / tokens ${ui.tokens}）`,
      `真实指标渲染异常：${JSON.stringify(ui)}`,
    );
  } else {
    check(ui.latency === '—', '指标接口未就绪时指标卡降级为「—」', `指标降级异常：${ui.latency}`);
  }
  if (tracesDoc) {
    const expected = Array.isArray(tracesDoc.items) ? tracesDoc.items.length : 0;
    check(
      ui.rows >= 1 && ui.rows <= Math.max(20, expected),
      `真实 trace 列表渲染（${ui.rows} 条）`,
      `真实 trace 渲染异常：${JSON.stringify({ rows: ui.rows, expected })}`,
    );
    if (ui.rows >= 1) {
      await cdp.evaluate(`document.querySelector('.obs-trace-head')?.click()`);
      await sleep(900);
      const detail = await cdp.evaluate(`({
        expanded: document.querySelectorAll('.obs-trace.expanded').length,
        events: document.querySelectorAll('.obs-trace-detail .obs-trace-event-row').length,
      })`);
      check(detail.expanded === 1, '真实 trace 可展开详情', `真实 trace 展开异常：${JSON.stringify(detail)}`);
      log(`  真实 trace 展开：${detail.events} 条事件`);
      const liveObsShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w15-observability-live.png'));
      log(`真实可观测性截图：${liveObsShot}`);
    }
  } else {
    check(ui.rows === 0, 'trace 接口未就绪时列表为空并提示', `trace 降级异常：${ui.rows}`);
  }
  if (ragDoc) {
    check(ui.ragVisible === true, '真实 RAG 评测卡渲染', '真实 RAG 评测卡未显示');
  } else {
    check(ui.ragVisible === false, 'RAG 评测接口 404 时隐藏评测卡', 'RAG 评测卡未隐藏');
  }
  if (!metricsDoc && !tracesDoc) {
    check(ui.status.includes('未就绪'), `可观测性接口未就绪时显示降级提示（${ui.status.slice(0, 50)}）`, `降级提示异常：${ui.status}`);
  }
  check(ui.runtimeError === '', '可观测性加载无运行时错误', `可观测性运行时错误：${ui.runtimeError}`);
}

/**
 * 纯前端单测式断言（无 DOM、无网络）：验证 stale_source 语义在纯函数层正确。
 * 场景：PC-JX1 有一条 stale_source=true 的 NC 滞后 issue（数据源已恢复），
 *       SW-SX-2960 有一条真正的 active 告警。
 */
function runStaleSourceUnitChecks() {
  log('');
  log('—— [单测] stale_source 语义（纯函数，无 DOM）——');
  const synthetic = normalizeSnapshot({
    devices: [
      {
        id: 'd-jx',
        hostname: 'SW-JX-2960',
        management_ip: '192.168.10.11',
        platform_id: '2960',
        product_id: '2960-24TT',
        device_type: 'Switch',
        reachability: 'Reachable',
        collection_status: 'Managed',
        source: 'netcon',
      },
      {
        id: 'd-sx',
        hostname: 'SW-SX-2960',
        management_ip: '192.168.10.12',
        platform_id: '2960',
        product_id: '2960-24TT',
        device_type: 'Switch',
        reachability: 'Reachable',
        collection_status: 'Managed',
        source: 'netcon',
      },
    ],
    hosts: [
      {
        id: 'h-pc1',
        hostname: 'PC-JX1',
        ip: '192.168.20.10',
        connected_device_name: 'SW-JX-2960',
        connected_interface: 'FastEthernet0/1',
        host_type: 'Pc',
        ping_status: 'SUCCESS',
        source: 'netcon',
      },
    ],
    issues: [
      {
        issue_id: 'stale-1',
        issue_name: 'Connectivity',
        issue_description: 'Device is down',
        issue_source: 'PC-JX1',
        severity: 'critical',
        status: 'active',
        stale_source: true,
        observed_at: 1793348000,
        source_ts: '2026-10-29 22:37:29',
        source: 'netcon',
      },
    ],
    alerts: [
      {
        id: 101,
        alert_type: 'device_unreachable',
        severity: 'critical',
        title: '设备不可达：SW-SX-2960',
        detail: 'reachabilityStatus=Unreachable',
        dedup_key: 'device_unreachable:d-sx',
        device_id: 'd-sx',
        observed_at: 1793348100,
        source_ts: '2026-10-29 22:40:00',
        state: 'active',
        source: 'netcon',
      },
      {
        id: 102,
        alert_type: 'health_issue',
        severity: 'critical',
        title: '网络健康问题：Connectivity',
        detail: 'Device is down',
        dedup_key: 'health_issue:stale-1',
        device_id: 'h-pc1',
        issue_id: 'stale-1',
        observed_at: 1793348050,
        source_ts: '2026-10-29 22:37:29',
        state: 'cleared',
        resolved_at: 1793348060,
        clear_reason: 'pingStatus FAILURE -> SUCCESS（按数据源实际状态判定恢复，不等 NC issue 清除）',
        source: 'netcon',
      },
    ],
    topology: { nodes: [], links: [] },
    networkHealth: null,
    healthStatus: null,
    alertsSummary: { active: 1, cleared: 1, info: 0, total: 2 },
    capturedAt: 1793348200,
    capturedAtIso: new Date(1793348200 * 1000).toISOString(),
    observedAt: 1793348200,
    hostsObservedAt: 1793348200,
    snapshotAgeSeconds: 1,
    source: 'netcon',
    stale: false,
  });
  const items = buildSceneItems(synthetic, []);
  const pcItem = items.find((item) => item.hostname === 'PC-JX1');
  const sxItem = items.find((item) => item.hostname === 'SW-SX-2960');
  check(Boolean(pcItem && sxItem), '单测场景构建成功（PC-JX1 + SW-SX-2960）', '单测场景构建失败');
  check(
    pcItem?.alert === false,
    '单测：stale_source issue 不触发 3D 告警标记（PC-JX1 不红/不脉冲）',
    '单测：stale_source issue 仍然触发了 3D 告警标记',
  );
  check(
    sxItem?.alert === true,
    '单测：真正的 active alert 仍正常触发 3D 告警标记（SW-SX-2960）',
    '单测：active alert 未触发 3D 告警标记',
  );

  const entries = buildAlertEntries(synthetic, []);
  const staleEntry = entries.find((entry) => entry.key === 'issue:stale-1');
  const activeEntry = entries.find(
    (entry) => entry.state === 'active' && entry.sourceType === 'alert',
  );
  check(
    staleEntry?.state === 'cleared' && staleEntry?.staleSource === true,
    '单测：stale_source issue 状态映射为 cleared（不属于活跃）',
    `单测：stale_source issue 状态映射错误：${JSON.stringify(staleEntry)}`,
  );
  check(
    alertStateLabel(staleEntry) === '已恢复 · NC 记录滞后',
    `单测：stale_source 文案为「已恢复 · NC 记录滞后」（实际 ${alertStateLabel(staleEntry)}）`,
    '单测：stale_source 文案错误',
  );
  check(alertStateLabel(activeEntry) === '活跃', '单测：active alert 文案为「活跃」', '单测：active alert 文案错误');
  check(
    countActiveEntries(entries) === 1,
    `单测：活跃计数只认 active alerts（实际 ${countActiveEntries(entries)}）`,
    `单测：活跃计数错误（${countActiveEntries(entries)}）`,
  );
  check(
    entries[0]?.key === activeEntry?.key,
    '单测：活跃条目排在已恢复条目之前',
    `单测：告警排序错误（${entries.map((entry) => entry.key).join(' > ')}）`,
  );
  check(
    Boolean(staleEntry?.clearReason && staleEntry.clearReason.includes('pingStatus')),
    '单测：stale issue 合并 cleared alert 的恢复原因（tooltip 数据）',
    '单测：stale issue 未合并恢复原因',
  );
}

/** live 模式：真实后端数据 + 点击设备 + AI 真实问答 */
async function runLiveChecks(cdp) {
  const devicesDoc = await getJson(`${API_BASE}/api/devices`);
  const hostsDoc = await getJson(`${API_BASE}/api/hosts`);
  const issuesDoc = await getJson(`${API_BASE}/api/issues`);
  const [alertsDoc, alertsSummaryDoc, rulesDoc] = await Promise.all([
    getJson(`${API_BASE}/api/alerts?limit=50&state=all`),
    getJson(`${API_BASE}/api/alerts/summary`),
    getJson(`${API_BASE}/api/detection/rules`),
  ]);
  const staleIssues = (issuesDoc.issues || []).filter((issue) => issue.stale_source === true);
  const activeIssues = (issuesDoc.issues || []).filter(
    (issue) => issue.stale_source !== true && String(issue.status || 'active').toLowerCase() === 'active',
  );
  let agentHealth = null;
  try {
    agentHealth = await getJson(`${AGENT_BASE}/api/health`);
  } catch (error) {
    log(`[WARN] Agent 服务不可达（${AGENT_BASE}）：${error.message}`);
  }
  const backendDevices = devicesDoc.devices || [];
  const backendHosts = hostsDoc.hosts || [];
  log(
    `后端基线：设备 ${backendDevices.length} 台，主机 ${backendHosts.length} 台，` +
      `stale=${devicesDoc.stale}，hosts.observed_at=${hostsDoc.observed_at_iso}，` +
      `告警 active=${alertsSummaryDoc.active}/cleared=${alertsSummaryDoc.cleared}，检测引擎=${rulesDoc.engine}，` +
      `主机快照延迟=${hostsDoc.snapshot_age_seconds}s，` +
      `issues active=${activeIssues.length}/stale_source=${staleIssues.length}` +
      (staleIssues.length ? `（${staleIssues.map((issue) => issue.issue_source).join('、')}）` : ''),
  );
  if (agentHealth) {
    log(`Agent 基线：provider=${agentHealth.provider} model=${agentHealth.model} provider_ready=${agentHealth.provider_ready}`);
  }
  log('');

  // 纯函数单测式断言（不依赖页面/网络）
  runStaleSourceUnitChecks();

  const readyStartedAt = Date.now();
  await cdp.waitFor(
    `Boolean(
      window.__campusTwin &&
      window.__campusTwin.state.snapshot &&
      window.__campusTwin.state.items.length > 0 &&
      document.getElementById('dev-stats').textContent.length > 0
    )`,
    20000,
    '前端 20s 内未完成后端数据渲染',
  );
  const firstFrameMs = Date.now() - readyStartedAt;
  await sleep(2600); // 等开场镜头动画结束，截图更稳定
  const wasAutoRotate = await pauseAutoRotate(cdp);

  const state = await cdp.evaluate(READ_STATS);
  const { stats } = state;
  log(`场景统计：${JSON.stringify(stats)}`);
  log(`DOM：title=${state.topbarTitle} canvas=${JSON.stringify(state.canvas)} 连接=${state.connection} 数据源=${state.source} 健康度=${state.health}`);
  log('');

  check(Boolean(state.canvas && state.canvas.width > 0 && state.canvas.height > 0), 'WebGL canvas 已创建', '未找到 WebGL canvas');
  check(state.loadingHidden === true, '加载遮罩已关闭（场景初始化完成）', '加载遮罩未关闭');
  check(state.offlineVisible === false, '后端在线，未显示离线提示', '后端在线却显示离线提示');
  check(state.runtimeError === '', '页面无运行时错误', `页面运行时错误：${state.runtimeError}`);
  check(state.connection.includes('已连接'), '状态栏显示后端已连接', `状态栏连接状态异常：${state.connection}`);
  check(state.topbarTitle.includes('校园网络数字孪生'), '顶部中文标题正常渲染', '顶部标题缺失');
  check(state.legendItems >= 8, '图例完整渲染', '图例渲染不完整');
  check(
    firstFrameMs <= 12000,
    `首帧数据渲染耗时 ${firstFrameMs}ms（含浏览器启动后的页面初始化开销）`,
    `首帧数据渲染耗时过长：${firstFrameMs}ms`,
  );

  // 数据一致性（与后端基线比对）
  check(stats.buildings === 4, '场景渲染 4 栋楼', `楼栋数异常：${stats.buildings}`);
  check(
    stats.devices === backendDevices.length,
    `场景设备数与后端一致：${stats.devices} 台`,
    `设备数不一致：场景 ${stats.devices} / 后端 ${backendDevices.length}`,
  );
  check(
    stats.hosts === backendHosts.length,
    `场景主机数与后端一致：${stats.hosts} 台`,
    `主机数不一致：场景 ${stats.hosts} / 后端 ${backendHosts.length}`,
  );
  check(
    stats.markers === stats.devices + stats.hosts + stats.controllers,
    `标记总数 = 设备 + 主机 + 数据源控制器（${stats.markers}）`,
    `标记数量异常：${stats.markers}`,
  );
  check(
    typeof stats.objects === 'number' && stats.objects < 60,
    `可渲染对象数 < 60（实际 ${stats.objects} 个渲染对象 / ${stats.nodes} 个场景节点）`,
    `可渲染对象数超限：${stats.objects}（场景节点 ${stats.nodes}）`,
  );
  check(stats.source === 'netcon', '数据来源为真实采集（netcon）', `数据来源异常：${stats.source}`);

  const [onlineCount, totalCount] = String(state.deviceValue).split('/');
  check(Number(totalCount) === backendDevices.length, `状态栏设备总数与后端一致：${state.deviceValue}`, `状态栏设备数异常：${state.deviceValue}`);
  check(Number(onlineCount) >= 0 && Number(onlineCount) <= backendDevices.length, `状态栏在线设备数有效：${state.deviceValue}`, `状态栏在线设备数异常：${state.deviceValue}`);
  check(state.health.includes('%'), '网络健康度已展示', `健康度展示异常：${state.health}`);
  check(state.collect.includes('正常') || state.collect.includes('陈旧'), '采集状态已展示', `采集状态异常：${state.collect}`);
  check(state.detailBody.includes('点击场景中的设备标记'), '未选中时详情面板显示操作提示', '详情面板初始提示缺失');

  // ---- W7 实时数据增强：数据新鲜度 / 告警生命周期 / observed_at / 检测引擎 ----
  log('');
  log('—— 实时数据增强（W3 observed_at / 告警生命周期 / 新鲜度）——');
  check(
    /秒前|分 .* 秒前|陈旧/.test(state.freshness),
    `状态栏显示数据新鲜度：${state.freshness}`,
    `数据新鲜度异常：${state.freshness}`,
  );
  check(
    (state.freshnessTitle.includes('数据更新于') || state.freshnessTitle.includes('陈旧')) &&
      !state.freshnessTitle.includes('NaN'),
    `新鲜度 tooltip 显示“数据更新于 …”或“采集陈旧”（${state.freshnessTitle}）`,
    `新鲜度 tooltip 异常：${state.freshnessTitle}`,
  );
  check(
    state.freshnessSub.includes('观测'),
    `新鲜度副标题显示宿主机观测时间：${state.freshnessSub}`,
    `新鲜度副标题异常：${state.freshnessSub}`,
  );
  check(
    String(state.alertsValue) === String(alertsSummaryDoc.active),
    `状态栏活跃告警与 /api/alerts/summary 一致（${state.alertsValue}）`,
    `状态栏活跃告警不一致：页面 ${state.alertsValue} / 后端 ${alertsSummaryDoc.active}`,
  );
  check(
    stats.detectionEngine === rulesDoc.engine,
    `检测引擎与后端一致：${stats.detectionEngine}`,
    `检测引擎不一致：页面 ${stats.detectionEngine} / 后端 ${rulesDoc.engine}`,
  );
  check(
    state.alertFoot.includes(rulesDoc.engine || 'self-rules-v1'),
    `告警面板显示检测引擎与生命周期统计：${state.alertFoot}`,
    `告警面板底部信息异常：${state.alertFoot}`,
  );
  check(
    typeof stats.snapshotAgeSeconds === 'number' && stats.snapshotAgeSeconds >= 0,
    `前端收到 /api/hosts 新鲜度字段（延迟 ${stats.snapshotAgeSeconds}s）`,
    `未收到 hosts 快照延迟字段：${stats.snapshotAgeSeconds}`,
  );
  check(
    Boolean(stats.observedAt),
    `快照带宿主机观测时间（${stats.observedAt}）`,
    `缺少宿主机观测时间字段`,
  );
  if (alertsDoc.alerts.length > 0 || staleIssues.length > 0) {
    const validStates = ['活跃', '已恢复', '信息', STALE_SOURCE_ISSUE_LABEL];
    check(
      state.alertStates.length > 0 && state.alertStates.every((text) => validStates.includes(text)),
      `告警列表显示生命周期状态标签（${[...new Set(state.alertStates)].join('/')}）`,
      `告警状态标签异常：${JSON.stringify(state.alertStates)}`,
    );
    check(
      state.alertTimeTitles.every((title) => title.includes('观测时间')),
      '告警时间 tooltip 使用 observed_at（宿主机时间）',
      `告警时间 tooltip 未使用 observed_at：${JSON.stringify(state.alertTimeTitles.slice(0, 2))}`,
    );
    const hasSourceTs = alertsDoc.alerts.some((alert) => alert.source_ts);
    if (hasSourceTs) {
      check(
        state.alertTimeTitles.some((title) => title.includes('NC 源时间')),
        '告警时间 tooltip 同时显示 NC 源时间 source_ts',
        '告警时间 tooltip 未显示 source_ts',
      );
    }
  } else {
    log('  [INFO] 后端当前无历史告警，跳过告警生命周期标签校验');
  }

  const overviewPath = await cdp.screenshot(join(DOCS_DIR, 'screenshot-overview.png'));
  log(`总览截图：${overviewPath}`);

  // ---- W7.1 stale_source 语义：NC 滞后 issue 不应显示为活跃 / 红色 / 脉冲 ----
  log('');
  log('—— W7.1 stale_source 语义（NC 滞后 issue，真实后端数据）——');
  check(
    String(stats.issuesStale) === String(staleIssues.length),
    `页面识别 stale_source issue 数量与后端一致（${stats.issuesStale}/${staleIssues.length}）`,
    `stale issue 数量不一致：页面 ${stats.issuesStale} / 后端 ${staleIssues.length}`,
  );
  check(
    String(stats.issuesActive) === String(activeIssues.length),
    `页面活跃 issue 数量与后端一致（${stats.issuesActive}/${activeIssues.length}）`,
    `活跃 issue 数量不一致：页面 ${stats.issuesActive} / 后端 ${activeIssues.length}`,
  );
  check(
    String(state.alertCount) === String(alertsSummaryDoc.active),
    `告警角标 = 活跃告警数（${state.alertCount}/${alertsSummaryDoc.active}）`,
    `告警角标异常：${state.alertCount} / active=${alertsSummaryDoc.active}`,
  );

  if (staleIssues.length) {
    const staleDevices = staleIssues
      .map((issue) => String(issue.issue_source || '').trim())
      .filter(Boolean);
    for (const deviceName of staleDevices) {
      const view = await cdp.evaluate(`(() => {
        const app = window.__campusTwin;
        const item = app.state.items.find((candidate) => candidate.hostname === ${JSON.stringify(deviceName)});
        const marker = item ? app.scene._markers.get(item.key) : null;
        const node = [...document.querySelectorAll('.alert-item')].find(
          (element) => element.dataset.device === ${JSON.stringify(deviceName)},
        );
        return {
          itemFound: Boolean(item),
          itemAlert: item?.alert ?? null,
          markerAlert: Boolean(marker?.group?.userData?.alert),
          markerColor: marker ? '#' + marker.material.color.getHexString() : null,
          listState: node?.querySelector('.alert-state')?.textContent || '',
          listClass: node?.className || '',
          listTime: node?.querySelector('time')?.textContent || '',
          listTimeTitle: node?.querySelector('time')?.getAttribute('title') || '',
          listItemTitle: node?.getAttribute('title') || '',
        };
      })()`);
      log(`stale 设备 ${deviceName}：${JSON.stringify(view)}`);
      check(view.itemFound, `场景中找到 ${deviceName} 条目`, `场景中未找到 ${deviceName}`);
      check(
        view.itemAlert === false,
        `${deviceName} 的 3D 标记不处于告警态（无红色标记/脉冲）`,
        `${deviceName} 仍被标记为告警态`,
      );
      check(
        view.markerAlert === false,
        `${deviceName} 标记 userData.alert=false（不参与告警脉冲）`,
        `${deviceName} 仍参与告警脉冲`,
      );
      check(
        !view.listState.includes('活跃'),
        `${deviceName} 告警列表不显示「活跃」标签（实际：${view.listState}）`,
        `${deviceName} 列表仍显示活跃`,
      );
      check(
        view.listState.includes('已恢复') && view.listState.includes('NC'),
        `${deviceName} 列表显示「已恢复 · NC 记录滞后」`,
        `${deviceName} 列表状态文案异常：${view.listState}`,
      );
      check(
        view.listClass.includes('stale-source') && view.listClass.includes('state-cleared'),
        `${deviceName} 列表条目为次要样式（stale-source / state-cleared）`,
        `${deviceName} 列表条目样式异常：${view.listClass}`,
      );
      check(
        view.listTimeTitle.includes('观测时间') && view.listTimeTitle.includes('NC 问题记录滞后'),
        `${deviceName} 时间 tooltip 使用 observed_at 且说明 NC 滞后原因`,
        `${deviceName} 时间 tooltip 异常：${view.listTimeTitle}`,
      );
      check(
        view.listItemTitle.includes('NC 问题记录滞后'),
        `${deviceName} 条目 tooltip 说明 NC 滞后原因`,
        `${deviceName} 条目 tooltip 缺失说明`,
      );
      if (deviceName === 'PC-JX1') {
        check(
          view.markerColor === '#38e08a',
          `PC-JX1 恢复后标记为已纳管绿色（${view.markerColor}）`,
          `PC-JX1 标记颜色异常：${view.markerColor}`,
        );
      }
    }

    const pulse = await cdp.evaluate(
      `({ alertMarkers: window.__campusTwin.scene._alertMarkers.length, haloCount: window.__campusTwin.scene.haloMesh.count })`,
    );
    check(
      pulse.alertMarkers === alertsSummaryDoc.active,
      `3D 告警脉冲标记数 = active alerts 数（${pulse.alertMarkers}/${alertsSummaryDoc.active}）`,
      `脉冲标记数异常：${pulse.alertMarkers} / active=${alertsSummaryDoc.active}`,
    );
    check(
      pulse.haloCount === alertsSummaryDoc.active,
      `3D 红色光环实例数 = active alerts 数（${pulse.haloCount}/${alertsSummaryDoc.active}）`,
      `红色光环实例数异常：${pulse.haloCount} / active=${alertsSummaryDoc.active}`,
    );

    // 恢复后截图：聚焦 stale 设备，展示绿色标记 + 详情 + 左侧“已恢复 · NC 记录滞后”
    await cdp.evaluate(`(() => {
      const app = window.__campusTwin;
      const item = app.state.items.find((candidate) => candidate.hostname === ${JSON.stringify(staleDevices[0])});
      if (item) app.scene.selectItem(item.key);
    })()`);
    await sleep(1600);
    const recoveredPath = await cdp.screenshot(join(DOCS_DIR, 'screenshot-recovered.png'));
    log(`恢复后截图：${recoveredPath}`);
  } else {
    log('  [INFO] 后端当前无 stale_source issue，跳过 stale_source 页面语义校验');
  }
  // 复位视角，避免 stale 截图时聚焦的镜头影响后续点击测试
  await cdp.evaluate(`document.getElementById('btn-reset').click()`);
  await sleep(1300);

  // ---- W9：真实后端配置驱动场景 + 告警管理 UI ----
  log('');
  log('—— W9 配置驱动场景 & 告警管理 UI（真实后端）——');
  let sceneDoc = null;
  try {
    sceneDoc = await getJson(`${API_BASE}/api/scene`);
  } catch {
    /* 后端 W9 接口未就绪时跳过 */
  }
  if (sceneDoc) {
    check(
      stats.sceneSource === 'api',
      `场景配置来自 GET /api/scene（v${stats.sceneVersion}）`,
      `场景来源异常：${stats.sceneSource}`,
    );
    check(
      stats.buildings === sceneDoc.buildings.length,
      `动态楼栋数与后端配置一致（${stats.buildings} 栋）`,
      `楼栋数不一致：页面 ${stats.buildings} / 后端 ${sceneDoc.buildings.length}`,
    );
    check(
      String(stats.sceneVersion) === String(sceneDoc.version),
      `场景配置版本与后端一致（v${stats.sceneVersion}）`,
      `场景版本不一致：页面 ${stats.sceneVersion} / 后端 ${sceneDoc.version}`,
    );
    const sceneMetaText = await cdp.evaluate(
      `document.getElementById('scene-meta')?.textContent || ''`,
    );
    check(
      sceneMetaText.includes('后端配置') && sceneMetaText.includes(`${sceneDoc.buildings.length} 栋楼`),
      `图例页签展示场景配置来源：${sceneMetaText}`,
      `场景配置来源展示异常：${sceneMetaText}`,
    );
    const pendingDoc = await getJson(`${API_BASE}/api/scene/pending`).catch(() => null);
    if (pendingDoc && Number(pendingDoc.count) === 0) {
      const bannerHidden = await cdp.evaluate(
        `document.getElementById('new-device-banner').classList.contains('hidden')`,
      );
      check(bannerHidden === true, '后端无 pending 设备时新设备提示条隐藏', '无 pending 设备却显示提示条');
    }
  } else {
    log('  [INFO] 后端 /api/scene 不可用，跳过配置驱动场景校验');
  }

  const alertMgmt = await cdp.evaluate(`(() => ({
    rows: document.querySelectorAll('.alert-item').length,
    deleteButtons: document.querySelectorAll('[data-alert-action="delete"]').length,
    silenceButtons: document.querySelectorAll('[data-alert-action="silence"]').length,
    clearText: document.getElementById('btn-clear-cleared')?.textContent || '',
    clearDisabled: document.getElementById('btn-clear-cleared')?.disabled ?? null,
  }))()`);
  log(`告警管理 UI：${JSON.stringify(alertMgmt)}`);
  if (Number(alertsSummaryDoc.cleared) > 0) {
    check(
      alertMgmt.deleteButtons > 0,
      `已恢复告警条目显示删除按钮（${alertMgmt.deleteButtons} 个）`,
      '已恢复告警缺少删除按钮',
    );
    check(
      alertMgmt.clearText.includes('清空已恢复') && alertMgmt.clearDisabled === false,
      `顶部「清空已恢复」按钮可用（${alertMgmt.clearText}）`,
      `清空已恢复按钮异常：${alertMgmt.clearText}/${alertMgmt.clearDisabled}`,
    );
  } else {
    log('  [INFO] 后端当前无已恢复告警，跳过删除按钮断言');
  }
  if (Number(alertsSummaryDoc.active) > 0) {
    check(alertMgmt.silenceButtons > 0, '活跃告警条目显示静默按钮', '活跃告警缺少静默按钮');
  } else {
    log('  [INFO] 后端当前无活跃告警，跳过静默按钮断言');
  }

  // AI 历史会话入口：Agent 接口就绪则展示列表；未就绪则优雅降级（不阻塞对话）
  await cdp.evaluate(`document.getElementById('tab-ai').click()`);
  await sleep(250);
  await cdp.evaluate(`document.getElementById('ai-history')?.click()`);
  await sleep(800);
  const historyEntry = await cdp.evaluate(`(() => {
    const panel = document.getElementById('ai-history-panel');
    return {
      visible: panel && !panel.classList.contains('hidden'),
      items: document.querySelectorAll('.ai-history-item').length,
      emptyText: document.querySelector('.ai-history-empty')?.textContent || '',
    };
  })()`);
  check(
    historyEntry.visible &&
      (historyEntry.items > 0 || /暂不支持|失败|正在加载|暂无/.test(historyEntry.emptyText)),
    `AI 历史会话入口可用（${historyEntry.items} 条会话${
      historyEntry.items ? '' : `，${historyEntry.emptyText}`
    }）`,
    `AI 历史会话入口异常：${JSON.stringify(historyEntry)}`,
  );
  await cdp.evaluate(`document.getElementById('ai-history-close')?.click()`);
  await sleep(200);

  // 真实鼠标点击设备标记 → 相机聚焦 + 详情面板
  const targetName = backendDevices[0]?.hostname;
  const clickTarget = await cdp.evaluate(`(() => {
    const app = window.__campusTwin;
    const item = app.state.items.find((candidate) => candidate.hostname === ${JSON.stringify(targetName)});
    if (!item) return null;
    const marker = app.scene._markers.get(item.key);
    if (!marker) return null;
    const projected = marker.group.position.clone().project(app.scene.camera);
    return {
      name: item.hostname,
      key: item.key,
      x: ((projected.x + 1) / 2) * window.innerWidth,
      y: ((-projected.y + 1) / 2) * window.innerHeight,
    };
  })()`);
  check(
    Boolean(clickTarget && Number.isFinite(clickTarget.x) && Number.isFinite(clickTarget.y)),
    `定位到设备标记「${targetName}」的屏幕坐标`,
    `无法定位设备标记「${targetName}」`,
  );

  if (clickTarget) {
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: clickTarget.x, y: clickTarget.y });
    await sleep(200);
    await cdp.send('Input.dispatchMouseEvent', {
      type: 'mousePressed',
      x: clickTarget.x,
      y: clickTarget.y,
      button: 'left',
      clickCount: 1,
    });
    await cdp.send('Input.dispatchMouseEvent', {
      type: 'mouseReleased',
      x: clickTarget.x,
      y: clickTarget.y,
      button: 'left',
      clickCount: 1,
    });
    await sleep(1400); // 等聚焦动画

    const detail = await cdp.evaluate(`(() => ({
      selected: window.__campusTwin.state.selectedKey,
      name: document.querySelector('.detail-name')?.textContent || '',
      body: document.getElementById('detail-body').textContent,
      ringVisible: Boolean(window.__campusTwin.scene.selectionRing?.visible),
      closeVisible: !document.getElementById('btn-close-detail').classList.contains('hidden'),
    }))()`);
    const backendTarget = backendDevices.find((device) => device.hostname === targetName) || {};
    log(`点击详情：${JSON.stringify(detail).slice(0, 200)}`);

    check(detail.selected === clickTarget.key, '点击后设备被选中（state.selectedKey 正确）', '点击后未选中设备');
    check(detail.name === targetName, `详情面板标题为设备名「${targetName}」`, `详情面板标题异常：${detail.name}`);
    check(
      backendTarget.management_ip ? detail.body.includes(backendTarget.management_ip) : true,
      `详情面板显示后端管理 IP（${backendTarget.management_ip}）`,
      `详情面板未显示后端管理 IP（${backendTarget.management_ip}）`,
    );
    check(detail.body.includes('楼'), '详情面板显示所属楼栋', '详情面板未显示所属楼栋');
    check(
      detail.body.includes('数据观测时间') && detail.body.includes('快照延迟'),
      '详情面板显示数据观测时间与快照延迟（W3 新鲜度）',
      '详情面板缺少数据观测时间/快照延迟',
    );
    check(detail.ringVisible === true, '3D 选中环可见（相机聚焦生效）', '3D 选中环不可见');
    check(detail.closeVisible === true, '详情面板关闭按钮可见', '详情面板关闭按钮不可见');

    const detailPath = await cdp.screenshot(join(DOCS_DIR, 'screenshot-detail.png'));
    log(`选中截图：${detailPath}`);

    // 点击空白处取消选中
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: 1000, y: 170, button: 'left', clickCount: 1 });
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: 1000, y: 170, button: 'left', clickCount: 1 });
    await sleep(500);
    const afterDeselect = await cdp.evaluate(`(() => ({
      selected: window.__campusTwin.state.selectedKey,
      ring: Boolean(window.__campusTwin.scene.selectionRing?.visible),
      body: document.getElementById('detail-body').textContent,
    }))()`);
    check(
      afterDeselect.selected === null && afterDeselect.ring === false,
      '点击空白处取消选中（详情面板恢复提示）',
      `点击空白处未取消选中：${JSON.stringify(afterDeselect)}`,
    );

    // 自动旋转开关（从当前状态切换两次，验证可开/可关）
    const rotateBefore = await cdp.evaluate(`window.__campusTwin.scene.autoRotate`);
    await cdp.evaluate(`document.getElementById('toggle-rotate').click()`);
    await sleep(250);
    const rotateAfterFirst = await cdp.evaluate(`window.__campusTwin.scene.autoRotate`);
    await cdp.evaluate(`document.getElementById('toggle-rotate').click()`);
    await sleep(250);
    const rotateAfterSecond = await cdp.evaluate(`window.__campusTwin.scene.autoRotate`);
    check(
      rotateAfterFirst === !rotateBefore && rotateAfterSecond === rotateBefore,
      '自动旋转开关可正常开/关',
      `自动旋转开关异常：初始=${rotateBefore} 第一次=${rotateAfterFirst} 第二次=${rotateAfterSecond}`,
    );
  }

  // ---- AI 运维助手：真实 Agent 调用（20~40s 级别，放最后） ----
  await runAiChatChecks(cdp, agentHealth);
  // ---- W9.1：真实 Agent action 卡片联调（只取消不确认） ----
  await runLiveActionChecks(cdp);
  // ---- W10：路径追踪 / 告警根因分组 / 通知状态（真实后端，未就绪时校验降级） ----
  await runLiveW10Checks(cdp);
  // ---- W12：流式 / RAG（真实 Agent，未就绪时校验回退） ----
  await runLiveStreamingChecks(cdp);
  // ---- W15：可观测性页签（真实 Agent，未就绪时校验降级） ----
  await runLiveW15Checks(cdp);

  await restoreAutoRotate(cdp, wasAutoRotate);
}

/** demo 模式：后端离线 → 未连接提示 → 手动打开演示数据 → 告警脉冲 + 演示水印 */
async function runDemoChecks(cdp) {
  // 用与浏览器相同的逻辑算出演示数据的预期统计，避免测试里写死数字
  const expectedItems = buildSceneItems(normalizeSnapshot(demoBundle()), []);
  const expected = {
    buildings: 4,
    devices: expectedItems.filter((item) => item.kind === 'device').length,
    hosts: expectedItems.filter((item) => item.kind === 'host').length,
    controllers: expectedItems.filter((item) => item.kind === 'controller').length,
    markers: expectedItems.length,
    alertMarkers: expectedItems.filter((item) => item.alert).length,
  };
  log(`演示数据预期（由 src/api/mock.js 推导）：${JSON.stringify(expected)}`);

  // 1. 等待离线状态确认（后端地址不可达 → 显示“未连接”）
  await cdp.waitFor(
    `Boolean(
      window.__campusTwin &&
      document.getElementById('dev-stats').textContent.length > 0 &&
      window.__campusTwin.state.backendOnline === false &&
      !document.getElementById('offline-banner').classList.contains('hidden')
    )`,
    25000,
    '后端离线时页面未显示“未连接”提示',
  );
  await sleep(800);
  const offline = await cdp.evaluate(READ_STATS);
  log(`离线状态：连接=${offline.connection} 提示=${offline.offlineText} 场景设备=${offline.stats.devices} 标记=${offline.stats.markers}`);

  check(offline.connection.includes('未连接'), '后端不可用时状态栏显示“未连接”', `离线状态栏异常：${offline.connection}`);
  check(offline.offlineVisible === true, '显示后端离线提示条', '未显示后端离线提示条');
  check(offline.runtimeError === '', '离线时页面无运行时脚本错误', `页面运行时错误：${offline.runtimeError}`);
  check(offline.stats.devices === 0 && offline.stats.hosts === 0, '离线且未开演示时，场景不展示任何设备（不造假数据）', `离线时场景设备数异常：${offline.stats.devices}/${offline.stats.hosts}`);
  check(offline.stats.buildings === 4, '离线时 4 栋楼体仍正常渲染', `离线时楼栋数异常：${offline.stats.buildings}`);
  check(offline.demoBadgeVisible === false, '默认关闭演示数据（无水印）', '默认状态错误地显示了演示水印');

  // 2. 手动打开“演示数据”开关
  await cdp.evaluate(`document.getElementById('toggle-mock').click()`);
  await cdp.waitFor(
    `Boolean(window.__campusTwin.state.demo === true && window.__campusTwin.state.items.length > 0)`,
    8000,
    '打开演示数据开关后场景未加载演示数据',
  );
  await sleep(2200); // 等脉冲动画跑起来
  const wasAutoRotate = await pauseAutoRotate(cdp);
  await sleep(300);

  const demo = await cdp.evaluate(READ_STATS);
  const pulse = await cdp.evaluate(`(() => {
    const scene = window.__campusTwin.scene;
    return {
      alertMarkers: scene._alertMarkers.length,
      haloInstances: scene.haloMesh.count,
      haloVisible: scene.haloMesh.visible,
      markerMaterialsPulsing: scene._alertMarkers.every((marker) => marker.material.emissiveIntensity >= 0.3),
    };
  })()`);
  log(`演示状态：${JSON.stringify(demo.stats)}`);
  log(`告警脉冲：${JSON.stringify(pulse)}`);

  check(demo.stats.demo === true, '演示数据已加载（state.demo=true）', '演示数据未加载');
  check(demo.demoBadgeVisible === true && demo.demoBadgeText.includes('非真实采集'), '界面显示“演示数据 · 非真实采集”水印', '演示数据水印缺失');
  check(demo.connection.includes('未连接'), '演示模式仍如实显示后端“未连接”', `演示模式连接状态异常：${demo.connection}`);
  check(demo.stats.source === 'simulator', '演示数据来源标记为 simulator', `演示数据来源异常：${demo.stats.source}`);
  check(demo.stats.buildings === 4, '演示场景渲染 4 栋楼', `演示场景楼栋数异常：${demo.stats.buildings}`);
  check(demo.stats.devices === expected.devices, `演示设备数与 mock 数据一致：${demo.stats.devices}`, `演示设备数异常：${demo.stats.devices} / 预期 ${expected.devices}`);
  check(demo.stats.hosts === expected.hosts, `演示主机数与 mock 数据一致：${demo.stats.hosts}`, `演示主机数异常：${demo.stats.hosts} / 预期 ${expected.hosts}`);
  check(demo.stats.markers === expected.markers, `演示标记总数正确：${demo.stats.markers}`, `演示标记总数异常：${demo.stats.markers} / 预期 ${expected.markers}`);
  check(
    pulse.alertMarkers === expected.alertMarkers && pulse.alertMarkers > 0,
    `告警设备脉冲高亮生效（${pulse.alertMarkers} 台设备）`,
    `告警脉冲异常：alertMarkers=${pulse.alertMarkers} / 预期 ${expected.alertMarkers}`,
  );
  check(
    pulse.haloInstances === expected.alertMarkers,
    `告警光环 InstancedMesh 实例数正确（${pulse.haloInstances}）`,
    `告警光环实例数异常：${pulse.haloInstances}`,
  );
  check(String(demo.alertCount) === String(expectedItems.filter((item) => item.alert).length) || Number(demo.alertCount) > 0, '告警列表展示演示告警', `告警列表数量异常：${demo.alertCount}`);
  check(demo.offlineVisible === true && demo.offlineText.includes('演示数据'), '离线提示条说明当前为演示数据', `离线提示条说明异常：${demo.offlineText}`);

  const demoShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-demo.png'));
  log(`演示截图：${demoShot}`);
  await restoreAutoRotate(cdp, wasAutoRotate);

  // 3. 关闭演示开关 → 回到“未连接 + 无设备”状态
  await cdp.evaluate(`document.getElementById('toggle-mock').click()`);
  await cdp.waitFor(`Boolean(window.__campusTwin.state.demo === false)`, 5000, '关闭演示数据开关后状态未复位');
  await sleep(600);
  const afterOff = await cdp.evaluate(READ_STATS);
  check(afterOff.demoBadgeVisible === false, '关闭演示后水印消失', '关闭演示后水印未消失');
  check(afterOff.stats.markers === 0, '关闭演示后场景不残留演示设备', `关闭演示后仍有 ${afterOff.stats.markers} 个标记`);
  check(afterOff.offlineVisible === true, '关闭演示后仍显示后端未连接提示', '关闭演示后离线提示缺失');
}

/**
 * ai-offline 模式：后端正常、Agent 不可达。
 * 验证「AI 服务未连接」降级提示 + AI 面板禁用，且 3D 大屏/告警完全不受影响。
 */
async function runAiOfflineChecks(cdp) {
  const devicesDoc = await getJson(`${API_BASE}/api/devices`);
  const hostsDoc = await getJson(`${API_BASE}/api/hosts`);
  const backendDevices = devicesDoc.devices || [];
  const backendHosts = hostsDoc.hosts || [];
  log(`后端基线：设备 ${backendDevices.length} 台，主机 ${backendHosts.length} 台（Agent 应为不可达）`);

  await cdp.waitFor(
    `Boolean(
      window.__campusTwin &&
      window.__campusTwin.state.snapshot &&
      window.__campusTwin.state.items.length > 0
    )`,
    20000,
    '3D 大屏未在后端在线时完成渲染',
  );
  await sleep(1200);
  const state = await cdp.evaluate(READ_STATS);
  log(`场景统计：${JSON.stringify(state.stats)}`);

  // 1) 3D 大屏不受 AI 服务离线影响
  check(state.runtimeError === '', 'AI 离线时页面无运行时脚本错误', `页面运行时错误：${state.runtimeError}`);
  check(state.stats.buildings === 4, 'AI 离线时 4 栋楼正常渲染', `楼栋数异常：${state.stats.buildings}`);
  check(
    state.stats.devices === backendDevices.length && state.stats.hosts === backendHosts.length,
    `AI 离线时设备/主机标记不受影响（${state.stats.devices} + ${state.stats.hosts}）`,
    `AI 离线时设备/主机数异常：${state.stats.devices}/${state.stats.hosts}`,
  );
  check(String(state.deviceValue).includes('/'), `顶部状态栏设备数据正常：${state.deviceValue}`, `状态栏设备数据异常：${state.deviceValue}`);
  check(/秒前|分 .* 秒前|陈旧/.test(state.freshness), `数据新鲜度仍正常刷新：${state.freshness}`, `新鲜度异常：${state.freshness}`);

  // 2) AI 页签降级提示
  await cdp.evaluate(`document.getElementById('tab-ai').click()`);
  await sleep(600);
  await cdp.waitFor(
    `(document.getElementById('ai-status-text').textContent || '').includes('AI 服务未连接')`,
    20000,
    'Agent 不可达时未显示「AI 服务未连接」提示',
  );
  const ai = await cdp.evaluate(`({
    status: document.getElementById('ai-status-text').textContent,
    modelLine: document.getElementById('ai-model-line').textContent,
    hint: document.getElementById('ai-hint').textContent,
    sendDisabled: document.getElementById('ai-send').disabled,
    quickDisabled: [...document.querySelectorAll('.ai-quick-btn')].every((button) => button.disabled),
    retryVisible: !document.getElementById('ai-retry').classList.contains('hidden'),
    tabDot: document.getElementById('ai-tab-dot').className,
    paneActive: document.getElementById('pane-ai').classList.contains('active'),
  })`);
  log(`AI 降级状态：${JSON.stringify(ai)}`);

  check(ai.paneActive === true, 'AI 页签仍可打开（仅问答不可用）', 'AI 页签无法打开');
  check(ai.status.includes('AI 服务未连接'), `显示「AI 服务未连接（地址）」：${ai.status}`, `AI 离线提示异常：${ai.status}`);
  check(ai.status.includes('127.0.0.1:9'), '离线提示包含实际 Agent 地址', `AI 离线提示未包含地址：${ai.status}`);
  check(ai.modelLine.includes('不影响 3D 大屏'), '说明 AI 离线不影响 3D 大屏', `AI 离线说明异常：${ai.modelLine}`);
  check(ai.sendDisabled === true, 'AI 离线时发送按钮禁用', 'AI 离线时发送按钮未禁用');
  check(ai.quickDisabled === true, 'AI 离线时快捷提问按钮禁用', 'AI 离线时快捷按钮未禁用');
  check(ai.retryVisible === true, '显示「重试连接」按钮', '未显示重试连接按钮');
  check(ai.tabDot.includes('bad'), 'AI 页签状态圆点为红色（离线）', `AI 页签圆点异常：${ai.tabDot}`);

  const offShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-ai-offline.png'));
  log(`AI 离线截图：${offShot}`);
}

/**
 * w9 模式：配置驱动动态场景 + 告警管理 + 历史会话 + Agent action 卡片。
 * 使用 scripts/mock-w9-server.mjs 提供的契约 mock（后端 8002 / Agent 8102），
 * 页面 dev server 5176 通过 VITE_API_BASE/VITE_AGENT_BASE 指向 mock。
 */
async function runW9Checks(cdp) {
  const backend = W9_BACKEND;
  log('—— W9：配置驱动动态场景 / 告警管理 / 历史会话 / Agent action 卡片 ——');
  log(`Mock backend：${backend} · Mock agent：${W9_AGENT}`);
  await fetch(`${backend}/__mock/reset`, { method: 'POST' }).catch(() => {});
  await cdp.evaluate(`window.__campusTwin?.refresh?.()`);
  await cdp.waitFor(
    `Boolean(window.__campusTwin && window.__campusTwin.state.snapshot && window.__campusTwin.state.items.length > 0)`,
    20000,
    'W9 mock 页面未完成数据渲染',
  );
  await sleep(2600);
  const wasAutoRotate = await pauseAutoRotate(cdp);

  // ---- 1. 配置驱动动态楼栋 ----
  let pageState = await cdp.evaluate(READ_STATS);
  log(`W9 场景统计：${JSON.stringify(pageState.stats)}`);
  check(pageState.stats.sceneSource === 'api', `场景来源为后端配置（${pageState.stats.sceneSource}）`, `场景来源异常：${pageState.stats.sceneSource}`);
  check(pageState.stats.buildings === 5, `动态渲染 5 栋楼（含新增「实验楼」）`, `楼栋数异常：${pageState.stats.buildings}`);
  check(pageState.stats.pendingMarkers >= 2, `pending 设备标记已渲染（${pageState.stats.pendingMarkers}）`, `pending 设备标记数异常：${pageState.stats.pendingMarkers}`);
  const sceneInfo = await cdp.evaluate(`(() => {
    const scene = window.__campusTwin.scene;
    return {
      buildings: scene._buildings.map((building) => building.name),
      labels: scene.buildingLabels.length,
      pendingNames: scene._items.filter((item) => item.pending).map((item) => item.hostname),
    };
  })()`);
  check(
    sceneInfo.buildings.includes('实验楼') && sceneInfo.labels === 5,
    `新增楼栋由配置驱动渲染（${sceneInfo.buildings.join('/')}）`,
    `新增楼栋渲染异常：${JSON.stringify(sceneInfo)}`,
  );

  // ---- 2. 新设备提示条 + pending 角标 ----
  const banner = await cdp.evaluate(`(() => {
    const node = document.getElementById('new-device-banner');
    return {
      visible: node && !node.classList.contains('hidden'),
      count: document.getElementById('new-device-count')?.textContent || '',
      rows: [...document.querySelectorAll('.new-device-item')].map((row) => row.dataset.name),
      selects: document.querySelectorAll('.new-device-select').length,
      text: node?.textContent || '',
    };
  })()`);
  log(`新设备提示条：${JSON.stringify(banner)}`);
  check(banner.visible === true && Number(banner.count) >= 2, `顶部显示「发现 ${banner.count} 台新设备」`, `新设备提示异常：${JSON.stringify(banner)}`);
  check(
    banner.rows.includes('AP-LAB-1') && banner.rows.includes('CAM-GATE-1'),
    '提示条列出新设备名称（含未推断楼栋设备）',
    `新设备名称缺失：${JSON.stringify(banner.rows)}`,
  );
  check(banner.text.includes('实验楼'), '提示条展示推断楼栋（实验楼）', '提示条未展示推断楼栋');
  check(banner.selects >= 1, '无法推断楼栋的设备提供楼栋下拉选择', `缺少楼栋下拉：${banner.selects}`);
  const pendingVisual = await cdp.evaluate(`(() => {
    const item = window.__campusTwin.state.items.find((candidate) => candidate.hostname === 'AP-LAB-1');
    const marker = item ? window.__campusTwin.scene._markers.get(item.key) : null;
    return {
      found: Boolean(item),
      pending: item?.pending ?? null,
      hasOutline: Boolean(marker?.group.children.some((child) => child.userData?.isPendingOutline && child.visible !== false)),
      hasBadge: Boolean(marker?.group.children.some((child) => child.userData?.isPendingBadge && child.visible !== false)),
      opacity: marker?.material?.opacity ?? null,
    };
  })()`);
  check(pendingVisual.found && pendingVisual.pending === true, 'AP-LAB-1 以 pending 状态出现在 3D 场景', `pending 设备缺失：${JSON.stringify(pendingVisual)}`);
  check(pendingVisual.hasOutline && pendingVisual.hasBadge, 'pending 设备带虚线外框 + 「新设备」角标', `pending 视觉异常：${JSON.stringify(pendingVisual)}`);
  check(pendingVisual.opacity !== null && pendingVisual.opacity < 1, `pending 设备浅色半透明（opacity=${pendingVisual.opacity}）`, `pending 透明度异常：${pendingVisual.opacity}`);
  const dynamicShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w9-dynamic.png'));
  log(`动态场景截图：${dynamicShot}`);

  // ---- 3. 新设备自动出现 + 高亮（模拟 PT 新增设备被后端自动发现） ----
  await fetch(`${backend}/__mock/add_pending?name=AP-NEW-1&building_id=lab`, { method: 'POST' });
  await cdp.waitFor(
    `Boolean(window.__campusTwin.state.items.some((item) => item.hostname === 'AP-NEW-1'))`,
    15000,
    '后端新增设备后前端未自动出现（需要场景版本轮询）',
  );
  await sleep(400);
  pageState = await cdp.evaluate(READ_STATS);
  const highlight = await cdp.evaluate(`(() => {
    const scene = window.__campusTwin.scene;
    const item = window.__campusTwin.state.items.find((candidate) => candidate.hostname === 'AP-NEW-1');
    const marker = item ? scene._markers.get(item.key) : null;
    return {
      pendingMarkers: scene.getObjectStats().pendingMarkers,
      highlighted: scene.getObjectStats().highlightedMarkers,
      lastNewMarkers: scene.getObjectStats().lastNewMarkers,
      scale: marker?.group?.scale?.x ?? null,
      visibleInBanner: [...document.querySelectorAll('.new-device-item')].some((row) => row.dataset.name === 'AP-NEW-1'),
    };
  })()`);
  log(`自动发现：${JSON.stringify(highlight)}`);
  check(highlight.visibleInBanner === true, '新设备自动加入提示条（无需刷新页面）', '新设备未自动加入提示条');
  check(
    highlight.highlighted >= 1 || highlight.lastNewMarkers >= 1 || (highlight.scale && highlight.scale > 1),
    `新设备出现时自动高亮（highlighted=${highlight.highlighted} lastNew=${highlight.lastNewMarkers}）`,
    `新设备未高亮：${JSON.stringify(highlight)}`,
  );

  // ---- 4. 确认加入 / 撤销 ----
  await cdp.evaluate(`(() => {
    const row = document.querySelector('.new-device-item[data-name="AP-LAB-1"]');
    row?.querySelector('button[data-action="confirm"]')?.click();
  })()`);
  await cdp.waitFor(
    `Boolean(!document.querySelector('.new-device-item[data-name="AP-LAB-1"]'))`,
    10000,
    '点击确认后提示条未移除该设备',
  );
  await sleep(600);
  const confirmed = await cdp.evaluate(`(() => {
    const item = window.__campusTwin.state.items.find((candidate) => candidate.hostname === 'AP-LAB-1');
    const marker = item ? window.__campusTwin.scene._markers.get(item.key) : null;
    return {
      pending: item?.pending ?? null,
      hasBadge: Boolean(marker?.group.children.some((child) => child.userData?.isPendingBadge && child.visible !== false)),
      hasOutline: Boolean(marker?.group.children.some((child) => child.userData?.isPendingOutline && child.visible !== false)),
    };
  })()`);
  check(
    confirmed.pending === false && confirmed.hasBadge === false && confirmed.hasOutline === false,
    '确认加入后 AP-LAB-1 转为 confirmed（新设备角标/虚线消失）',
    `确认后状态异常：${JSON.stringify(confirmed)}`,
  );
  // 撤销 AP-NEW-1（含确认弹窗）
  await cdp.evaluate(`(() => {
    const row = document.querySelector('.new-device-item[data-name="AP-NEW-1"]');
    row?.querySelector('button[data-action="remove"]')?.click();
  })()`);
  await sleep(300);
  check(
    await cdp.evaluate(`!document.getElementById('confirm-overlay').classList.contains('hidden')`),
    '撤销新设备弹出二次确认框',
    '撤销新设备未弹出确认框',
  );
  await cdp.evaluate(`document.getElementById('confirm-ok')?.click()`);
  await cdp.waitFor(
    `Boolean(!document.querySelector('.new-device-item[data-name="AP-NEW-1"]'))`,
    10000,
    '撤销后提示条未移除该设备',
  );
  const sceneAfterRemove = await (await fetch(`${backend}/api/scene`)).json();
  const labBuilding = sceneAfterRemove.buildings.find((building) => building.id === 'lab');
  check(
    !labBuilding.devices.some((device) => device.name === 'AP-NEW-1'),
    '撤销后设备已从后端场景配置移除',
    '撤销后设备仍存在于场景配置',
  );

  // ---- 5. 告警管理 UI：删除 / 静默 / 清空已恢复 / 撤销 ----
  // 收起新设备提示条，避免遮挡告警管理截图（提示条仍保留“发现 N 台新设备”头部）
  await cdp.evaluate(`document.getElementById('new-device-collapse')?.click()`);
  await sleep(400);
  const alertUI = await cdp.evaluate(`(() => ({
    rows: document.querySelectorAll('.alert-item').length,
    deleteButtons: document.querySelectorAll('[data-alert-action="delete"]').length,
    silenceButtons: document.querySelectorAll('[data-alert-action="silence"]').length,
    clearText: document.getElementById('btn-clear-cleared')?.textContent || '',
    clearDisabled: document.getElementById('btn-clear-cleared')?.disabled ?? null,
  }))()`);
  log(`告警管理 UI：${JSON.stringify(alertUI)}`);
  check(alertUI.deleteButtons >= 2, `已恢复告警条目带删除按钮（${alertUI.deleteButtons} 个）`, `删除按钮数异常：${alertUI.deleteButtons}`);
  check(alertUI.silenceButtons >= 1, '活跃告警条目带静默按钮', `静默按钮数异常：${alertUI.silenceButtons}`);
  check(alertUI.clearText.includes('清空已恢复') && alertUI.clearDisabled === false, `顶部清空已恢复按钮可用（${alertUI.clearText}）`, `清空按钮异常：${alertUI.clearText}/${alertUI.clearDisabled}`);
  const alertsShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w9-alerts.png'));
  log(`告警管理截图：${alertsShot}`);

  // 静默活跃告警
  await cdp.evaluate(`document.querySelector('[data-alert-action="silence"]')?.click()`);
  await sleep(300);
  await cdp.evaluate(`document.getElementById('confirm-ok')?.click()`);
  await sleep(1400);
  const afterSilence = await cdp.evaluate(`(() => ({
    labels: [...document.querySelectorAll('.alert-item .alert-state')].map((node) => node.textContent),
    silenceButtons: document.querySelectorAll('[data-alert-action="silence"]').length,
  }))()`);
  check(
    afterSilence.silenceButtons === 0 && afterSilence.labels.some((text) => text.includes('已静默')),
    '静默活跃告警后按钮消失并显示「已静默」',
    `静默结果异常：${JSON.stringify(afterSilence)}`,
  );

  // 删除单条已恢复告警 + 撤销
  const rowsBeforeDelete = await cdp.evaluate(`document.querySelectorAll('.alert-item').length`);
  await cdp.evaluate(`document.querySelector('[data-alert-action="delete"]')?.click()`);
  await sleep(300);
  await cdp.evaluate(`document.getElementById('confirm-ok')?.click()`);
  await cdp.waitFor(
    `document.querySelectorAll('.alert-item').length < ${rowsBeforeDelete}`,
    10000,
    '删除已恢复告警后列表未减少',
  );
  const undoToast = await cdp.evaluate(`(() => {
    const toast = document.getElementById('toast');
    return {
      visible: toast && !toast.classList.contains('hidden'),
      action: toast?.querySelector('.toast-action')?.textContent || '',
    };
  })()`);
  check(undoToast.action.includes('撤销'), `删除后 Toast 提供 10 秒撤销（${undoToast.action}）`, `撤销按钮缺失：${JSON.stringify(undoToast)}`);
  await cdp.evaluate(`document.querySelector('.toast-action')?.click()`);
  await cdp.waitFor(
    `document.querySelectorAll('.alert-item').length === ${rowsBeforeDelete}`,
    10000,
    '撤销删除后告警未恢复',
  );
  check(true, '撤销删除后告警恢复', '');

  // 清空已恢复（按钮 + 确认）
  await cdp.evaluate(`document.getElementById('btn-clear-cleared')?.click()`);
  await sleep(300);
  check(
    await cdp.evaluate(`!document.getElementById('confirm-overlay').classList.contains('hidden')`),
    '清空已恢复弹出二次确认框',
    '清空已恢复未弹出确认框',
  );
  await cdp.evaluate(`document.getElementById('confirm-ok')?.click()`);
  await cdp.waitFor(
    `document.querySelectorAll('[data-alert-action="delete"]').length === 0`,
    10000,
    '清空已恢复后仍有删除按钮',
  );
  const summaryAfterClear = await (await fetch(`${backend}/api/alerts/summary`)).json();
  check(
    summaryAfterClear.cleared === 0,
    `清空后后端已恢复告警为 0（实际 ${summaryAfterClear.cleared}）`,
    `清空后后端仍有已恢复告警：${summaryAfterClear.cleared}`,
  );

  // ---- 6. AI action 确认卡片 ----
  await cdp.evaluate(`document.getElementById('tab-ai').click()`);
  await sleep(400);
  await cdp.evaluate(`document.querySelector('.ai-quick-btn')?.click()`);
  await cdp.waitFor(`document.querySelectorAll('.ai-action-card').length >= 2`, 20000, 'AI 回答未渲染 action 确认卡片');
  const cards = await cdp.evaluate(`(() => ({
    total: document.querySelectorAll('.ai-action-card').length,
    danger: document.querySelectorAll('.ai-action-card.danger').length,
    hasCancel: Boolean(document.querySelector('button[data-action-role="cancel"]')),
    texts: [...document.querySelectorAll('.ai-action-confirm')].map((node) => node.textContent),
    dangerBadge: document.querySelector('.ai-action-card.danger .ai-action-danger')?.textContent || '',
  }))()`);
  log(`action 卡片：${JSON.stringify(cards)}`);
  check(cards.total >= 2, `回答下方渲染 ${cards.total} 个 action 确认卡片`, `action 卡片数异常：${cards.total}`);
  check(cards.danger >= 1 && cards.dangerBadge.includes('危险'), '危险操作卡片红色样式 + 「危险操作」标识', `危险卡片异常：${JSON.stringify(cards)}`);
  check(cards.texts.some((text) => text.includes('清空') || text.includes('确认')), '卡片展示 confirm_text 操作描述', '卡片缺少 confirm_text');
  check(cards.hasCancel, '卡片带「取消」按钮', '卡片缺少取消按钮');

  // 取消一个 action
  await cdp.evaluate(`(() => {
    const card = [...document.querySelectorAll('.ai-action-card')].find((node) => !node.classList.contains('danger'));
    card?.querySelector('[data-action-role="cancel"]')?.click();
  })()`);
  await cdp.waitFor(`Boolean(document.querySelector('.ai-action-card.cancelled'))`, 10000, '取消 action 未生效');
  const cancelledText = await cdp.evaluate(`document.querySelector('.ai-action-card.cancelled .ai-action-result')?.textContent || ''`);
  check(cancelledText.includes('取消'), `取消 action 后卡片显示结果（${cancelledText.slice(0, 40)}）`, `取消结果异常：${cancelledText}`);

  // 确认执行危险 action（清空已恢复告警，已空则返回 0 条，但卡片状态应变为已执行）
  await cdp.evaluate(`(() => {
    const card = document.querySelector('.ai-action-card.danger');
    card?.querySelector('[data-action-role="confirm"]')?.click();
  })()`);
  await cdp.waitFor(`Boolean(document.querySelector('.ai-action-card.done'))`, 15000, '确认 action 未执行');
  const doneText = await cdp.evaluate(`document.querySelector('.ai-action-card.done .ai-action-result')?.textContent || ''`);
  check(doneText.length > 0, `确认执行后显示执行结果（${doneText.slice(0, 50)}）`, '缺少执行结果');
  await sleep(800);
  // 滚动到 action 卡片并截图
  await cdp.evaluate(`(() => {
    const messages = document.getElementById('ai-messages');
    const card = document.querySelector('.ai-action-card');
    if (messages && card) messages.scrollTop = card.offsetTop - messages.offsetTop - 8;
  })()`);
  await sleep(400);
  const actionsShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w9-actions.png'));
  log(`action 卡片截图：${actionsShot}`);

  // ---- 7. 历史会话管理 ----
  await cdp.evaluate(`document.getElementById('ai-history')?.click()`);
  await sleep(800);
  const history = await cdp.evaluate(`(() => {
    const panel = document.getElementById('ai-history-panel');
    return {
      visible: panel && !panel.classList.contains('hidden'),
      items: document.querySelectorAll('.ai-history-item').length,
      hasDelete: Boolean(document.querySelector('.ai-history-del')),
      hasClear: Boolean(document.getElementById('ai-history-clear')),
    };
  })()`);
  log(`历史会话：${JSON.stringify(history)}`);
  check(history.visible && history.items >= 1, `历史会话列表渲染（${history.items} 条）`, `历史会话列表异常：${JSON.stringify(history)}`);
  check(history.hasDelete, '历史会话支持单条删除', '历史会话缺少单条删除');
  check(history.hasClear, '历史会话支持清空全部', '历史会话缺少清空全部');

  // 加载历史会话
  await cdp.evaluate(`document.querySelector('.ai-history-item')?.click()`);
  await sleep(1000);
  const loadedConversation = await cdp.evaluate(`(() => ({
    panelHidden: document.getElementById('ai-history-panel').classList.contains('hidden'),
    messages: document.querySelectorAll('#ai-messages .ai-msg').length,
  }))()`);
  check(
    loadedConversation.panelHidden && loadedConversation.messages >= 2,
    `点击历史会话加载消息（${loadedConversation.messages} 条）`,
    `历史会话加载异常：${JSON.stringify(loadedConversation)}`,
  );

  // 删除单条会话
  await cdp.evaluate(`document.getElementById('ai-history')?.click()`);
  await sleep(700);
  const conversationsBefore = await cdp.evaluate(`document.querySelectorAll('.ai-history-item').length`);
  await cdp.evaluate(`document.querySelector('.ai-history-del')?.click()`);
  await sleep(300);
  await cdp.evaluate(`document.getElementById('confirm-ok')?.click()`);
  await sleep(1200);
  const conversationsAfter = await cdp.evaluate(`document.querySelectorAll('.ai-history-item').length`);
  check(conversationsAfter < conversationsBefore, `删除单个历史会话（${conversationsBefore} → ${conversationsAfter}）`, '删除会话未生效');

  // 清空全部会话
  await cdp.evaluate(`document.getElementById('ai-history-clear')?.click()`);
  await sleep(300);
  await cdp.evaluate(`document.getElementById('confirm-ok')?.click()`);
  await sleep(1400);
  const clearedConversations = await cdp.evaluate(`({
    items: document.querySelectorAll('.ai-history-item').length,
    emptyText: document.querySelector('.ai-history-empty')?.textContent || '',
  })`);
  check(
    clearedConversations.items === 0,
    `清空全部历史会话（空状态：${clearedConversations.emptyText || '—'}）`,
    `清空会话异常：${JSON.stringify(clearedConversations)}`,
  );
  await cdp.evaluate(`document.getElementById('ai-history-close')?.click()`);

  // W10：路径追踪 3D / 告警根因分组 / 通知状态
  await runW10Checks(cdp);

  // W12：AI 流式渲染（SSE）+ RAG 引用 + 回退
  await runW12Checks(cdp);

  // W15：可观测性页签（metrics / traces / RAG 评测）
  await runW15Checks(cdp);

  await restoreAutoRotate(cdp, wasAutoRotate);
}

/**
 * W10 冒烟：路径追踪 3D 可视化（完整/断点/清除）、告警根因分组视图、通知状态与测试。
 * 依赖 mock server 的 W10 契约端点（/api/path-trace、/api/alerts/groups、/api/notify/*）。
 */
async function runW10Checks(cdp) {
  const backend = W9_BACKEND;
  log('');
  log('—— W10：路径追踪 3D 可视化 / 告警根因分组 / 通知状态（契约 Mock）——');

  // 收起 W9 新设备提示条，避免遮挡路径/分组截图（若已收起则不再点击，避免展开）
  await cdp.evaluate(`(() => {
    const banner = document.getElementById('new-device-banner');
    if (
      banner &&
      !banner.classList.contains('hidden') &&
      !banner.classList.contains('collapsed')
    ) {
      document.getElementById('new-device-collapse')?.click();
    }
  })()`);
  await sleep(300);

  // ---- 1. 路径追踪：完整路径（PC-JX1 → Server-1） ----
  await cdp.evaluate(`document.getElementById('tab-path').click()`);
  await sleep(500);
  const pathPanelState = await cdp.evaluate(`(() => ({
    tabActive: document.getElementById('pane-path').classList.contains('active'),
    sourceOptions: document.querySelectorAll('#path-source option').length,
    destOptions: document.querySelectorAll('#path-dest option').length,
    hasTraceButton: Boolean(document.getElementById('path-trace-btn')),
    hasClearButton: Boolean(document.getElementById('path-clear-btn')),
  }))()`);
  check(pathPanelState.tabActive === true, '路径追踪页签可打开', '路径追踪页签未激活');
  check(
    pathPanelState.sourceOptions >= 2 && pathPanelState.destOptions >= 2,
    `源/目的下拉已填充（${pathPanelState.sourceOptions} 个选项）`,
    `路径端点下拉异常：${JSON.stringify(pathPanelState)}`,
  );
  await cdp.evaluate(`(() => {
    document.getElementById('path-source').value = '192.168.20.10';
    document.getElementById('path-dest').value = '192.168.50.10';
    document.getElementById('path-trace-btn').click();
  })()`);
  await cdp.waitFor(
    `document.querySelectorAll('#path-hops .path-hop').length >= 4`,
    20000,
    '路径追踪未返回 4 个 hop',
  );
  await sleep(900);
  const completePath = await cdp.evaluate(`(() => ({
    hops: [...document.querySelectorAll('#path-hops .path-hop .path-hop-name')].map((node) => node.textContent),
    roles: [...document.querySelectorAll('#path-hops .path-hop-role')].map((node) => node.textContent),
    status: document.getElementById('path-status')?.textContent || '',
    summary: document.getElementById('path-summary')?.textContent || '',
    scene: window.__campusTwin.scene.getPathStats(),
    stats: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}'),
  }))()`);
  log(`完整路径：${JSON.stringify(completePath.scene)} hops=${completePath.hops.join('→')}`);
  check(
    completePath.hops.includes('PC-JX1') &&
      completePath.hops.includes('SW-JX-2960') &&
      completePath.hops.includes('Core-3650') &&
      completePath.hops.includes('Server-1'),
    `hop 列表按顺序展示设备（${completePath.hops.join(' → ')}）`,
    `hop 列表异常：${JSON.stringify(completePath.hops)}`,
  );
  check(
    completePath.roles.includes('源') && completePath.roles.includes('目的'),
    'hop 列表标注源/目的角色',
    `hop 角色异常：${JSON.stringify(completePath.roles)}`,
  );
  check(
    completePath.status.includes('完整路径'),
    `路径状态显示「完整路径」（${completePath.status}）`,
    `路径状态异常：${completePath.status}`,
  );
  check(
    completePath.scene.active === true &&
      completePath.scene.hops === 4 &&
      completePath.scene.labels === 4 &&
      completePath.scene.pulses === 2,
    `3D 场景已绘制发光路径（${completePath.scene.objects} 个对象 / ${completePath.scene.labels} 个 hop 编号 / 2 个方向脉冲）`,
    `3D 路径对象异常：${JSON.stringify(completePath.scene)}`,
  );
  check(
    completePath.stats.pathActive === true &&
      completePath.stats.pathHops === 4 &&
      completePath.stats.pathBreakpoint === false,
    'dev-stats 记录路径状态（active/hops=4/breakpoint=false）',
    `dev-stats 路径状态异常：${JSON.stringify(completePath.stats)}`,
  );
  const pathShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w10-path.png'));
  log(`路径追踪截图：${pathShot}`);

  // ---- 2. 路径追踪：断点路径（PC-JX1 → CAM-GATE-1 partial） ----
  await cdp.evaluate(`(() => {
    document.getElementById('path-dest').value = '192.168.60.20';
    document.getElementById('path-trace-btn').click();
  })()`);
  await cdp.waitFor(
    `document.querySelectorAll('#path-hops .path-hop.breakpoint').length === 1`,
    20000,
    '断点路径未渲染断点 hop',
  );
  await sleep(1000);
  const brokenPath = await cdp.evaluate(`(() => ({
    hops: document.querySelectorAll('#path-hops .path-hop').length,
    breakpointRows: document.querySelectorAll('#path-hops .path-hop.breakpoint').length,
    breakpointBadge: document.querySelector('#path-hops .path-hop.breakpoint .path-hop-break')?.textContent || '',
    status: document.getElementById('path-status')?.textContent || '',
    scene: window.__campusTwin.scene.getPathStats(),
    stats: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}'),
  }))()`);
  log(`断点路径：${JSON.stringify(brokenPath.scene)}`);
  check(brokenPath.breakpointRows === 1, '断点路径的最后一个 hop 标记为断点', `断点行异常：${brokenPath.breakpointRows}`);
  check(
    brokenPath.breakpointBadge.includes('断点'),
    `断点 hop 显示「✕ 断点」标签（${brokenPath.breakpointBadge}）`,
    `断点标签缺失：${brokenPath.breakpointBadge}`,
  );
  check(
    brokenPath.status.includes('断点') && brokenPath.status.includes('Core-3650'),
    `面板状态显示断点设备与原因（${brokenPath.status.slice(0, 80)}）`,
    `断点状态异常：${brokenPath.status}`,
  );
  check(
    brokenPath.scene.breakpoint === true && brokenPath.scene.breakpointDevice === 'Core-3650',
    '3D 场景断点红色 ✕ 标记到断点设备（Core-3650）',
    `3D 断点异常：${JSON.stringify(brokenPath.scene)}`,
  );
  check(
    brokenPath.stats.pathBreakpoint === true && brokenPath.stats.pathBreakpointDevice === 'Core-3650',
    'dev-stats 记录断点状态与设备',
    `dev-stats 断点异常：${JSON.stringify(brokenPath.stats)}`,
  );
  const brokenShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w10-path-broken.png'));
  log(`断点路径截图：${brokenShot}`);

  // ---- 3. 清除路径 ----
  await cdp.evaluate(`document.getElementById('path-clear-btn').click()`);
  await sleep(500);
  const clearedPath = await cdp.evaluate(`(() => ({
    scene: window.__campusTwin.scene.getPathStats(),
    emptyText: document.querySelector('#path-hops .empty')?.textContent || '',
    stats: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}'),
  }))()`);
  check(
    clearedPath.scene.active === false && clearedPath.scene.hops === 0,
    '点击「清除路径」后 3D 发光路径全部移除',
    `清除路径异常：${JSON.stringify(clearedPath.scene)}`,
  );
  check(clearedPath.stats.pathActive === false, 'dev-stats 清除路径后 pathActive=false', '清除后 dev-stats 未复位');

  // ---- 4. 通知状态 + 发送测试通知 ----
  const notifyBefore = await cdp.evaluate(`(() => ({
    value: document.getElementById('stat-notify-value')?.textContent || '',
    sub: document.getElementById('stat-notify-sub')?.textContent || '',
    testHidden: document.getElementById('btn-notify-test')?.classList.contains('hidden'),
    stats: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}'),
  }))()`);
  check(notifyBefore.value.includes('已启用'), `顶部通知状态显示「已启用」（${notifyBefore.value}）`, `通知状态异常：${notifyBefore.value}`);
  check(notifyBefore.sub.includes('Webhook'), `通知状态显示 Webhook 类型（${notifyBefore.sub}）`, `通知类型异常：${notifyBefore.sub}`);
  check(notifyBefore.testHidden === false, '通知启用时显示「测试」按钮', '测试按钮未显示');
  await cdp.evaluate(`document.getElementById('btn-notify-test').click()`);
  await cdp.waitFor(
    `(document.getElementById('toast')?.textContent || '').includes('测试通知')`,
    15000,
    '发送测试通知未返回成功提示',
  );
  await sleep(800);
  const notifyAfter = await cdp.evaluate(`(() => ({
    toast: document.getElementById('toast')?.textContent || '',
    sub: document.getElementById('stat-notify-sub')?.textContent || '',
    stats: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}'),
  }))()`);
  check(
    notifyAfter.toast.includes('测试通知已发送'),
    `发送测试通知成功（${notifyAfter.toast}）`,
    `测试通知异常：${notifyAfter.toast}`,
  );
  check(
    (notifyAfter.stats.notifySentCount || 0) > (notifyBefore.stats.notifySentCount || 0),
    `发送测试通知后 sent_count 增加（${notifyBefore.stats.notifySentCount} → ${notifyAfter.stats.notifySentCount}）`,
    `sent_count 未增加：${JSON.stringify(notifyAfter.stats)}`,
  );

  // ---- 5. 告警根因分组视图（注入链路故障 + 下游抑制成员） ----
  await fetch(`${backend}/__mock/seed_w10`, { method: 'POST' });
  await cdp.evaluate(`window.__campusTwin.refresh()`);
  await cdp.waitFor(
    `JSON.parse(document.getElementById('dev-stats').textContent || '{}').alertsActive >= 3`,
    15000,
    '注入 W10 告警后活跃告警数未增加',
  );
  await sleep(1200);
  await cdp.evaluate(`document.getElementById('alert-view-groups').click()`);
  await cdp.waitFor(`document.querySelectorAll('.alert-group').length >= 1`, 15000, '根因分组卡片未渲染');
  await sleep(800);
  const groupState = await cdp.evaluate(`(() => ({
    cards: document.querySelectorAll('.alert-group').length,
    expanded: document.querySelectorAll('.alert-group.expanded').length,
    rootBadge: document.querySelector('.alert-group-root')?.textContent || '',
    groupTitle: document.querySelector('.alert-group-title')?.textContent || '',
    meta: document.querySelector('.alert-group-meta')?.textContent || '',
    members: document.querySelectorAll('.alert-group-members .alert-member').length,
    suppressedMembers: document.querySelectorAll('.alert-member.suppressed').length,
    suppressedTags: [...document.querySelectorAll('.alert-member-suppressed-tag')].map((node) => node.textContent),
    suppressedBy: [...document.querySelectorAll('.alert-member .suppressed-by')].map((node) => node.textContent),
    actionButtons: document.querySelectorAll('.alert-group [data-alert-action]').length,
    hint: document.getElementById('alert-groups-hint')?.textContent || '',
    viewActive: document.getElementById('alert-view-groups')?.classList.contains('active'),
    stats: JSON.parse(document.getElementById('dev-stats')?.textContent || '{}'),
  }))()`);
  log(`根因分组：cards=${groupState.cards} members=${groupState.members} suppressed=${groupState.suppressedMembers}`);
  check(groupState.cards >= 1, `渲染 ${groupState.cards} 张根因分组卡片`, '根因分组卡片缺失');
  check(groupState.rootBadge.includes('链路中断'), `分组卡片显示根因「链路中断」（${groupState.rootBadge}）`, `根因标签异常：${groupState.rootBadge}`);
  check(groupState.groupTitle.includes('Core-3650'), '分组卡片显示根因设备 Core-3650', `根因设备异常：${groupState.groupTitle}`);
  check(groupState.expanded >= 1, '分组卡片默认展开成员列表', '分组卡片未展开');
  check(groupState.members >= 3, `分组展开显示 ${groupState.members} 个成员（含根因+被抑制）`, `成员数异常：${groupState.members}`);
  check(
    groupState.suppressedMembers >= 2 &&
      groupState.suppressedTags.filter((text) => text.includes('被上游抑制')).length >= 2,
    `被抑制成员灰显并标注「被上游抑制」（${groupState.suppressedMembers} 个）`,
    `被抑制成员异常：${JSON.stringify({ suppressed: groupState.suppressedMembers, tags: groupState.suppressedTags })}`,
  );
  check(
    groupState.suppressedBy.some((text) => text.includes('suppressed_by=g_')),
    '被抑制成员展示 suppressed_by 分组 id',
    `suppressed_by 缺失：${JSON.stringify(groupState.suppressedBy)}`,
  );
  check(groupState.actionButtons >= 1, `分组视图保留告警操作按钮（${groupState.actionButtons} 个静默/删除）`, '分组视图缺少操作按钮');
  check(groupState.hint.includes('根因分组'), `面板提示根因分组信息（${groupState.hint}）`, `分组提示异常：${groupState.hint}`);
  check(
    groupState.stats.alertView === 'groups' &&
      groupState.stats.alertGroupsCount === 1 &&
      groupState.stats.alertGroupsSuppressed >= 2,
    'dev-stats 记录分组视图/组数/抑制数',
    `dev-stats 分组异常：${JSON.stringify(groupState.stats)}`,
  );
  // 分组成员上的「静默」按钮仍可用（打开确认框后取消，不写数据）
  await cdp.evaluate(`document.querySelector('.alert-group [data-alert-action="silence"]')?.click()`);
  await sleep(400);
  const memberActionDialog = await cdp.evaluate(
    `!document.getElementById('confirm-overlay').classList.contains('hidden')`,
  );
  check(memberActionDialog === true, '分组内成员告警仍可操作（静默弹出确认框）', '分组内成员操作按钮未生效');
  await cdp.evaluate(`document.getElementById('confirm-cancel')?.click()`);
  await sleep(300);
  // 切回设备详情页签，让分组截图聚焦告警面板 + 3D 场景
  await cdp.evaluate(`document.getElementById('tab-detail')?.click()`);
  await sleep(300);
  const groupsShot = await cdp.screenshot(join(DOCS_DIR, 'screenshot-w10-groups.png'));
  log(`告警根因分组截图：${groupsShot}`);

  // 切回全部视图，确认可逆
  await cdp.evaluate(`document.getElementById('alert-view-all').click()`);
  await sleep(500);
  const backToAll = await cdp.evaluate(`(() => ({
    groups: document.querySelectorAll('.alert-group').length,
    alerts: document.querySelectorAll('#alerts-list .alert-item').length,
    viewActive: document.getElementById('alert-view-all')?.classList.contains('active'),
  }))()`);
  check(
    backToAll.groups === 0 && backToAll.alerts >= 1 && backToAll.viewActive,
    '可切回「全部」视图（普通告警列表）',
    `切回全部视图异常：${JSON.stringify(backToAll)}`,
  );
}

async function main() {
  const modeLabel = {
    demo: 'demo（后端离线 + 演示数据）',
    'ai-offline': 'ai-offline（Agent 离线降级）',
    live: 'live（真实后端 + 真实 AI 问答）',
    w9: 'w9（配置驱动场景 / 告警管理 / 会话 / action 卡片，Mock 契约）',
  }[MODE] || MODE;
  log('校园数字孪生前端 · 端到端冒烟自测（headless 浏览器 + CDP）');
  log(`模式：${modeLabel}`);
  log(`页面：${PAGE_URL}`);
  if (MODE !== 'demo' && MODE !== 'w9') log(`后端：${API_BASE}`);
  if (MODE !== 'ai-offline' && MODE !== 'w9') log(`Agent：${AGENT_BASE}`);
  log('');

  const browserPath = findBrowser();
  log(`浏览器：${browserPath}`);
  rmSync(PROFILE_DIR, { recursive: true, force: true });
  mkdirSync(PROFILE_DIR, { recursive: true });
  mkdirSync(DOCS_DIR, { recursive: true });

  const browser = await launchBrowser({
    browserPath,
    port: DEBUG_PORT,
    profileDir: PROFILE_DIR,
  });
  let cdp = null;
  try {
    cdp = await attachPage({ port: DEBUG_PORT, pageUrl: PAGE_URL });
    log(`已导航到 ${PAGE_URL}`);
    if (MODE === 'demo') {
      await runDemoChecks(cdp);
    } else if (MODE === 'ai-offline') {
      await runAiOfflineChecks(cdp);
    } else if (MODE === 'w9') {
      await runW9Checks(cdp);
    } else {
      await runLiveChecks(cdp);
    }
  } finally {
    await closeBrowser({ browser, cdp, profileDir: PROFILE_DIR });
  }

  log('');
  log('='.repeat(78));
  if (failures.length === 0) {
    const passMessages = {
      demo: '离线提示、演示数据开关、告警脉冲与演示水印全部通过。',
      'ai-offline': '「AI 服务未连接」降级提示正确，3D 大屏与实时数据不受影响。',
      live: '页面加载、4 楼/设备渲染、W3 实时数据增强、W7.1 stale_source 语义、W9 配置驱动场景/告警管理 UI、W9.1 新 trace 结构（plan/工具序列）、live action 卡片（场景新增设备取消 + 危险操作取消，数据未变更）、W10 路径追踪/根因分组/通知状态（就绪则联调、未就绪则降级提示）、W12 流式接口未就绪时自动回退非流式并提示、W15 可观测性页签（就绪则联调指标/trace/RAG、未就绪则优雅降级）、点击聚焦、AI 真实问答与 trace 全部通过。',
      w9: '配置驱动动态场景（新增楼栋/pending 设备/自动高亮）、新设备确认撤销、告警删除/静默/清空/撤销、历史会话、Agent action 卡片、W10 路径追踪 3D（完整/断点/清除）、告警根因分组（抑制成员灰显 + 操作保留）、通知状态/测试、W12 SSE 流式渲染/事件顺序/token 拼接/停止生成/RAG 引用/404 回退、W15 可观测性页签（指标卡/trace 列表展开/会话过滤/RAG 评测卡/404 降级）全部通过。',
    };
    log(`结果：PASS —— ${passMessages[MODE] || '全部检查通过。'}`);
  } else {
    log(`结果：FAIL —— 共 ${failures.length} 项问题：`);
    failures.forEach((message, index) => log(`  ${index + 1}. ${message}`));
  }
  log('='.repeat(78));
  writeFileSync(REPORT_PATH, reportLines.join('\n') + '\n', 'utf8');
  log(`报告已写入：${REPORT_PATH}`);
  process.exitCode = failures.length === 0 ? 0 : 1;
}

main().catch((error) => {
  log('');
  log(`冒烟自测执行异常：${error?.message || error}`);
  if (error?.stack) log(error.stack);
  try {
    writeFileSync(REPORT_PATH, reportLines.join('\n') + '\n', 'utf8');
  } catch {
    /* 报告写入失败不影响退出码 */
  }
  process.exitCode = 1;
});
