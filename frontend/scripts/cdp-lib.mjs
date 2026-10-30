/**
 * 端到端冒烟测试的公共工具（零依赖：使用 Node 内置 fetch / WebSocket 与 Edge/Chrome 的 CDP 协议）。
 *
 * 为什么不用 Playwright：本机无管理员权限，npmmirror 安装浏览器二进制不稳定；
 * 系统已有 Edge，headless + CDP 足够覆盖“页面加载 / 渲染 / 鼠标点击 / 截图”这些验收点。
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';

const EDGE_CANDIDATES = [
  process.env.EDGE_PATH,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].filter(Boolean);

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function findBrowser() {
  for (const candidate of EDGE_CANDIDATES) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  throw new Error('未找到 Edge/Chrome，可通过 EDGE_PATH 环境变量指定浏览器路径');
}

export async function waitForJson(url, timeoutMs = 25000) {
  const start = Date.now();
  let lastError = null;
  while (Date.now() - start < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch (error) {
      lastError = error;
    }
    await sleep(300);
  }
  throw new Error(`等待 ${url} 超时：${lastError?.message || '无响应'}`);
}

export async function getJson(url, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}：${url}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

function connectWebSocket(url, timeoutMs = 12000) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timer = setTimeout(() => reject(new Error('CDP WebSocket 连接超时')), timeoutMs);
    socket.addEventListener('open', () => {
      clearTimeout(timer);
      resolve(socket);
    });
    socket.addEventListener('error', () => {
      clearTimeout(timer);
      reject(new Error('CDP WebSocket 连接失败'));
    });
  });
}

/** 极简 CDP 客户端：按 id 匹配响应，忽略事件推送 */
export class CdpClient {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 0;
    this.pending = new Map();
    socket.addEventListener('message', (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!message.id || !this.pending.has(message.id)) return;
      const { resolve, reject } = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(`${message.error.message}（code=${message.error.code}）`));
      else resolve(message.result);
    });
  }

  send(method, params = {}, timeoutMs = 20000) {
    const id = (this.nextId += 1);
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (!this.pending.has(id)) return;
        this.pending.delete(id);
        reject(new Error(`CDP 调用超时：${method}`));
      }, timeoutMs);
    });
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(
        `页面脚本异常：${result.exceptionDetails.exception?.description || result.exceptionDetails.text}`,
      );
    }
    return result.result?.value;
  }

  async waitFor(expression, timeoutMs, timeoutMessage) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (await this.evaluate(expression)) return;
      await sleep(250);
    }
    throw new Error(timeoutMessage);
  }

  async screenshot(filePath) {
    const { writeFileSync } = await import('node:fs');
    const result = await this.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(filePath, Buffer.from(result.data, 'base64'));
    return filePath;
  }
}

/** 启动 headless 浏览器（独立临时用户目录，可多次重复运行） */
export async function launchBrowser({ browserPath, port, profileDir, width = 1600, height = 900 }) {
  const browser = spawn(
    browserPath,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-sandbox',
      '--no-first-run',
      '--no-default-browser-check',
      '--hide-scrollbars',
      '--force-device-scale-factor=1',
      `--window-size=${width},${height}`,
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profileDir}`,
      'about:blank',
    ],
    { stdio: 'ignore' },
  );
  await waitForJson(`http://127.0.0.1:${port}/json/version`);
  return browser;
}

/** 打开页面并返回 CDP 客户端（优先新开标签页，失败则复用已有页面目标） */
export async function attachPage({ port, pageUrl }) {
  let target = null;
  try {
    const response = await fetch(
      `http://127.0.0.1:${port}/json/new?${encodeURIComponent(pageUrl)}`,
      { method: 'PUT' },
    );
    if (response.ok) target = await response.json();
  } catch {
    /* 走下方兜底 */
  }
  if (!target) {
    const targets = await waitForJson(`http://127.0.0.1:${port}/json/list`);
    target = targets.find((candidate) => candidate.type === 'page');
  }
  if (!target?.webSocketDebuggerUrl) throw new Error('未找到可调试的页面目标');
  const socket = await connectWebSocket(target.webSocketDebuggerUrl);
  const cdp = new CdpClient(socket);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Page.navigate', { url: pageUrl });
  return cdp;
}

/** 关闭浏览器并清理临时用户目录（Windows 下文件锁可能延迟释放，做重试） */
export async function closeBrowser({ browser, cdp, profileDir }) {
  try {
    if (cdp) await cdp.send('Browser.close', {}, 5000).catch(() => {});
  } catch {
    /* 忽略 */
  }
  try {
    if (browser?.pid) {
      spawn('taskkill', ['/PID', String(browser.pid), '/T', '/F'], { stdio: 'ignore' });
    }
  } catch {
    /* 忽略 */
  }
  await sleep(800);
  const { rmSync } = await import('node:fs');
  for (let attempt = 0; attempt < 6; attempt += 1) {
    try {
      rmSync(profileDir, { recursive: true, force: true });
      return;
    } catch {
      await sleep(500);
    }
  }
}
