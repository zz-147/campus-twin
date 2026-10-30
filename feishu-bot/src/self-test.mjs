// -*- mode: js -*-
/** 离线自测：不连飞书、不依赖真实 Agent，验证指令路由/回复/安全逻辑。 */
import http from 'node:http';
import { config, assertAllowed } from './config.mjs';
import { createHandler } from './handler.mjs';
import { createAgentClient } from './agent-client.mjs';
import { routeMessage, HELP_TEXT } from './router.mjs';

let passed = 0;
let failed = 0;
function check(name, condition, extra = '') {
  if (condition) {
    passed += 1;
    console.log(`  PASS  ${name}`);
  } else {
    failed += 1;
    console.log(`  FAIL  ${name} ${extra}`);
  }
}

// ---------- 1) 路由单测 ----------
console.log('== 指令路由 ==');
check('帮助', routeMessage('帮助').kind === 'help');
check('状态', routeMessage('状态').question === '现在整体网络状态怎么样？');
check('告警', routeMessage('告警').kind === 'alerts');
check('路径', routeMessage('路径 PC-JX1 到 Server-1').question === 'PC-JX1 到 Server-1 的路径怎么走？');
check('读文件', routeMessage('读 configs/core-3650.txt').question === '帮我读一下 configs/core-3650.txt');
check('清空告警=写操作', routeMessage('把已恢复的告警清空').needsConfirm === true);
check('普通问题透传', routeMessage('交换机 Trunk 怎么配？').question === '交换机 Trunk 怎么配？');

// ---------- 2) 白名单 ----------
console.log('== 白名单 ==');
const cfgAllow = { ...config, allowedChatIds: ['chat_ok'], allowedUserIds: [] };
check('白名单内允许', assertAllowed(cfgAllow, { chatId: 'chat_ok', userId: 'u' }) === null);
check('白名单外拒绝', /未在白名单/.test(assertAllowed(cfgAllow, { chatId: 'chat_bad', userId: 'u' }) || ''));

// ---------- 3) handler（fake agent）----------
console.log('== handler 逻辑 ==');
const cfgTest = { ...config, rateLimitPerMin: 3, maxReplyChars: 120 };
let calls = [];
const fakeAgent = {
  async chat(question) {
    calls.push(question);
    if (question.includes('路径')) {
      return { ok: true, answer: '# 路径结果\n\nPC-JX1 → SW-JX-2960 → Core-3650 → Server-1（4 跳）', actions: [], provider: 'deepseek', model: 'deepseek-flash', elapsedMs: 1200 };
    }
    if (question.includes('清空')) {
      return { ok: true, answer: '将清空 38 条已恢复告警（需确认）。', actions: [{ id: 'act_1', type: 'clear_cleared_alerts', confirm_text: '将清空 38 条已恢复告警，确认执行？' }], provider: 'deepseek', model: 'deepseek-flash', elapsedMs: 900 };
    }
    if (question.includes('失败')) {
      return { ok: false, error: 'Agent 响应超时（240 秒）' };
    }
    if (question.includes('长文')) {
      return { ok: true, answer: '很长的回答。'.repeat(40), actions: [], provider: 'deepseek', model: 'deepseek-flash', elapsedMs: 900 };
    }
    return { ok: true, answer: '## 根因\n整体网络健康，5 台设备在线 [K1]。', actions: [], provider: 'deepseek', model: 'deepseek-flash', elapsedMs: 800 };
  },
};
const handler = createHandler({ cfg: cfgTest, agentClient: fakeAgent });

const r1 = await handler.handle({ chatId: 'c1', userId: 'u1', messageId: 'm1', text: '状态' });
check('状态回复含答案', r1[0].includes('根因') && r1[0].includes('5 台设备'));
check('状态最终调用 Agent', calls[calls.length - 1] === '现在整体网络状态怎么样？');

const r2 = await handler.handle({ chatId: 'c1', userId: 'u1', messageId: 'm2', text: '路径 PC-JX1 到 Server-1' });
check('路径回复含 4 跳', r2[0].includes('4 跳'));

const r3 = await handler.handle({ chatId: 'c1', userId: 'u1', messageId: 'm3', text: '把已恢复的告警清空' });
const r3All = r3.join('\n');
check('写操作提示确认', r3All.includes('需要你确认') && r3All.includes('Web 大屏'));

const r4 = await handler.handle({ chatId: 'c2', userId: 'u1', messageId: 'm4', text: '为什么失败' });
check('Agent 失败优雅提示', r4[0].includes('暂时无法回答'));

const r5 = await handler.handle({ chatId: 'c3', userId: 'u1', messageId: 'm5', text: '长文' });
check('长文自动分条', r5.length >= 2);

const dup = await handler.handle({ chatId: 'c1', userId: 'u1', messageId: 'm1', text: '状态' });
check('重复消息被去重', dup.length === 0);

const helpReply = await handler.handle({ chatId: 'c9', userId: 'u1', messageId: 'm9', text: '帮助' });
check('帮助不调用 Agent', helpReply[0] === HELP_TEXT);

// ---------- 4) 真实 HTTP 集成（本地 mock Agent 服务）----------
console.log('== Agent HTTP 集成 ==');
const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    const payload = JSON.parse(body || '{}');
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({
      answer: `收到：${payload.question}`,
      conversation_id: 'conv_test_1',
      provider: 'deepseek',
      model: 'deepseek-flash',
      elapsed_ms: 500,
      actions: [],
    }));
  });
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
try {
  const client = createAgentClient({ ...config, agentBase: `http://127.0.0.1:${port}`, agentTimeoutMs: 5000 });
  const first = await client.chat('测试问题', { chatId: 'chat1' });
  check('HTTP Agent 调用成功', first.ok && first.answer.includes('测试问题'));
  const second = await client.chat('第二个问题', { chatId: 'chat1' });
  check('会话续聊成功', second.ok && second.conversationId === 'conv_test_1');
} finally {
  server.close();
}

// ---------- 汇总 ----------
console.log(`\n结果：${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
