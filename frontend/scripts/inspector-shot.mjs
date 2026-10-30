/**
 * 专门重拍 MCP Inspector：点击服务器开关 → 等待工具列表 → 截图。
 * 用法：node scripts/inspector-shot.mjs "<inspector url with token>"
 */
import { mkdirSync } from 'node:fs';
import { launchBrowser, attachPage, closeBrowser, sleep, findBrowser } from './cdp-lib.mjs';

const URL_ = process.argv[2] || process.env.INSPECTOR_URL;
const OUT = process.env.SHOT_DIR || 'docs/portfolio';
mkdirSync(OUT, { recursive: true });

const port = Number(process.env.CDP_PORT || 9362);
const profileDir = `${process.env.TEMP || '.'}/inspector-shot-profile`;
const browser = await launchBrowser({
  browserPath: process.env.EDGE_PATH || findBrowser(),
  port,
  profileDir,
  width: 1600,
  height: 900,
});
let cdp = null;
try {
  if (!URL_) throw new Error('缺少 Inspector URL（带 token）');
  cdp = await attachPage({ port, pageUrl: URL_ });
  await cdp.waitFor(`document.body && document.body.innerText.length > 50`, 30000, 'Inspector 未加载');
  await sleep(3000);

  // 诊断页面上的可点击控件
  const info = await cdp.evaluate(`(() => {
    const out = {switches: 0, checkboxes: 0, buttons: []};
    out.switches = document.querySelectorAll('[role="switch"]').length;
    out.checkboxes = document.querySelectorAll('input[type="checkbox"]').length;
    out.buttons = Array.from(document.querySelectorAll('button')).slice(0, 8).map((b) => (b.innerText || '').trim()).filter(Boolean);
    return out;
  })()`);
  console.log('控件诊断:', JSON.stringify(info));

  // 点击第一个开关（role=switch 或 checkbox 或包含 Disconnected 的容器）
  const clicked = await cdp.evaluate(`(() => {
    const sw = document.querySelector('[role="switch"]');
    if (sw) { sw.click(); return 'switch'; }
    const cb = document.querySelector('input[type="checkbox"]');
    if (cb) { cb.click(); return 'checkbox'; }
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], div'));
    const target = candidates.find((el) => /disconnect/i.test(el.innerText || '') && (el.innerText || '').length < 40);
    if (target) { target.click(); return 'text'; }
    return '';
  })()`);
  console.log('点击结果:', clicked);
  await sleep(2000);

  // 等待工具名出现（最多 90 秒）
  const start = Date.now();
  let ready = false;
  let lastText = '';
  while (Date.now() - start < 90000) {
    const text = await cdp.evaluate(`document.body.innerText`);
    lastText = text;
    if (text.includes('get_devices')) { ready = true; break; }
    await sleep(1500);
  }
  console.log('工具列表就绪:', ready, '| 页面含 Connected:', lastText.includes('Connected'));

  // 依次点击 campus-twin -> Tools，等待工具名出现
  if (!ready) {
    for (let round = 0; round < 6; round += 1) {
      await cdp.evaluate(`(() => {
        const nodes = Array.from(document.querySelectorAll('a, button, [role="tab"], [role="menuitem"], [role="link"]'));
        const srv = nodes.find((n) => /^campus-twin$/i.test((n.innerText || '').trim()));
        if (srv) { srv.click(); return 'server'; }
        return '';
      })()`);
      await sleep(1200);
      await cdp.evaluate(`(() => {
        const nodes = Array.from(document.querySelectorAll('a, button, [role="tab"], [role="menuitem"], [role="link"]'));
        const tools = nodes.find((n) => /^tools$/i.test((n.innerText || '').trim()));
        if (tools) { tools.click(); return 'tools'; }
        return '';
      })()`);
      await sleep(2000);
      const text = await cdp.evaluate(`document.body.innerText`);
      if (text.includes('get_devices')) { ready = true; lastText = text; break; }
    }
    console.log('二次尝试后工具列表就绪:', ready);
  }
  await sleep(2000);
  await cdp.screenshot(`${OUT}/05-MCP-Server工具列表.png`);
  console.log('OK 05-MCP-Server工具列表.png');
  if (!ready) {
    const snippet = lastText.replace(/\s+/g, ' ').slice(0, 400);
    console.log('页面文本片段:', snippet);
  }
} catch (error) {
  console.error('失败:', error.message);
  process.exitCode = 1;
} finally {
  await closeBrowser({ browser, cdp, profileDir });
}
