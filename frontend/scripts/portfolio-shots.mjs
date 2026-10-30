/**
 * 作品集截图 v2：新建会话 → 拍「MCP 读配置」「流式输出」「RAG 引用」。
 * 修正：只等最后一条答案、流式需等到真的出现 token、截图前滚动到答案顶部。
 * 用法：node scripts/portfolio-shots.mjs
 */
import { mkdirSync } from 'node:fs';
import { launchBrowser, attachPage, closeBrowser, sleep, findBrowser } from './cdp-lib.mjs';

const WEB = process.env.WEB_BASE || 'http://127.0.0.1:5173/';
const OUT = process.env.SHOT_DIR || 'docs/portfolio';
mkdirSync(OUT, { recursive: true });

const port = Number(process.env.CDP_PORT || 9364);
const profileDir = `${process.env.TEMP || '.'}/portfolio-shots-profile2`;

async function newChat(cdp) {
  await cdp.evaluate(`(() => {
    const btn = document.querySelector('#ai-new');
    if (btn) btn.click();
    return true;
  })()`);
  await sleep(800);
}

async function typeAndSend(cdp, question) {
  await cdp.evaluate(`(() => {
    const input = document.querySelector('#ai-input');
    if (!input) throw new Error('找不到 AI 输入框');
    input.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(input, ${JSON.stringify(question)});
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`);
  await sleep(300);
  await cdp.evaluate(`document.querySelector('#ai-send').click()`);
}

/** 等待"最后一条消息"是完整答案（非 loading / 非 streaming） */
async function waitLastAnswerSettled(cdp, timeoutMs = 180000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const state = await cdp.evaluate(`(() => {
      const msgs = document.querySelector('#ai-messages');
      if (!msgs) return {ready: false, reason: 'no-messages'};
      const last = msgs.lastElementChild;
      if (!last) return {ready: false, reason: 'empty'};
      const cls = last.className || '';
      if (cls.includes('ai-msg-streaming')) return {ready: false, reason: 'streaming'};
      if (cls.includes('ai-msg-loading')) return {ready: false, reason: 'loading'};
      if (cls.includes('ai-msg-answer')) {
        const text = last.innerText || '';
        return {ready: text.length > 150, reason: 'answer:' + text.length};
      }
      return {ready: false, reason: 'last-not-answer'};
    })()`);
    if (state.ready) return true;
    await sleep(1200);
  }
  return false;
}

/** 等待流式过程出现足够 token（用于抓"流式中"画面） */
async function waitStreamingTokens(cdp, minChars = 150, timeoutMs = 150000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const state = await cdp.evaluate(`(() => {
      const last = document.querySelector('#ai-messages')?.lastElementChild;
      if (!last) return {len: 0, streaming: false};
      const markdown = last.querySelector('.ai-stream-markdown');
      const len = markdown ? (markdown.innerText || '').length : 0;
      return {len, streaming: (last.className || '').includes('ai-msg-streaming')};
    })()`);
    if (state.streaming && state.len >= minChars) return true;
    await sleep(500);
  }
  return false;
}

async function scrollLastIntoView(cdp) {
  await cdp.evaluate(`(() => {
    const msgs = document.querySelector('#ai-messages');
    if (!msgs) return false;
    const last = msgs.lastElementChild;
    if (last) last.scrollIntoView({block: 'start', inline: 'nearest'});
    return true;
  })()`);
  await sleep(500);
}

const browser = await launchBrowser({
  browserPath: process.env.EDGE_PATH || findBrowser(),
  port,
  profileDir,
  width: 1600,
  height: 900,
});
let cdp = null;
try {
  cdp = await attachPage({ port, pageUrl: WEB });
  await cdp.waitFor(`document.querySelectorAll('canvas').length > 0`, 30000, '大屏未加载');
  await sleep(4500);

  // ---------- 1. 大屏总览 ----------
  await cdp.screenshot(`${OUT}/01-平台总览.png`);
  console.log('OK 01-平台总览.png');

  // ---------- 2. Agent 通过 MCP 读真实配置文件 ----------
  await cdp.evaluate(`document.querySelector('#tab-ai').click()`);
  await sleep(900);
  await newChat(cdp);
  await typeAndSend(cdp, '帮我读一下 configs/core-3650.txt 里 VLAN 配置');
  const okMcp = await waitLastAnswerSettled(cdp);
  await scrollLastIntoView(cdp);
  await sleep(800);
  await cdp.screenshot(`${OUT}/02-Agent通过MCP读配置.png`);
  console.log(`OK 02-Agent通过MCP读配置.png（settled=${okMcp}）`);

  // ---------- 3. 流式输出（新会话，抓 token 过程中的画面） ----------
  await newChat(cdp);
  await typeAndSend(cdp, '宿舍楼 2960 交换机上联核心怎么配置？给出完整命令');
  const okStream = await waitStreamingTokens(cdp, 180, 150000);
  await scrollLastIntoView(cdp);
  await sleep(300);
  await cdp.screenshot(`${OUT}/03-AI流式输出.png`);
  console.log(`OK 03-AI流式输出.png（streaming-tokens=${okStream}）`);

  // ---------- 4. RAG 引用（等完整答案，展开参考知识） ----------
  const okRag = await waitLastAnswerSettled(cdp);
  await scrollLastIntoView(cdp);
  await sleep(400);
  const citationCount = await cdp.evaluate(`(() => {
    const details = document.querySelectorAll('#ai-messages details.ai-citations');
    const last = details[details.length - 1];
    if (last) { last.open = true; last.scrollIntoView({block: 'center'}); }
    return details.length;
  })()`);
  await sleep(700);
  await cdp.screenshot(`${OUT}/04-RAG引用.png`);
  console.log(`OK 04-RAG引用.png（settled=${okRag}, citations=${citationCount}）`);
} catch (error) {
  console.error('截图失败:', error.message);
  process.exitCode = 1;
} finally {
  await closeBrowser({ browser, cdp, profileDir });
  console.log('完成，输出目录:', OUT);
}
