// -*- mode: js -*-
/** 本地模拟：不连飞书，直接输入文本 → 走完整处理链路 → 打印回复（便于开发/演示）。 */
import { config } from './config.mjs';
import { createHandler } from './handler.mjs';
import { createFakeSender } from './feishu.mjs';

const args = process.argv.slice(2);
const text = args.join(' ').trim() || '状态';

const sender = createFakeSender();
const handler = createHandler({ cfg: config });

console.log(`[mock] Agent=${config.agentBase}`);
console.log(`[mock] 输入：${text}`);
const replies = await handler.handle({ chatId: 'mock-chat', userId: 'mock-user', messageId: `mock-${Date.now()}`, text });
if (!replies.length) {
  console.log('[mock] 无回复（可能是重复消息或被限流）');
} else {
  for (const reply of replies) {
    console.log('\n----- 回复 -----');
    console.log(reply);
  }
}
