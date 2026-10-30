// -*- mode: js -*-
/** 配置加载：支持 .env 文件 + 环境变量；不打印任何密钥。 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** 极简 .env 解析（KEY=VALUE，支持 # 注释与行尾空白） */
function loadDotEnv(file) {
  const out = {};
  if (!existsSync(file)) return out;
  for (const rawLine of readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const idx = line.indexOf('=');
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

const dotenv = loadDotEnv(resolve(ROOT, '.env'));

function env(name, fallback = '') {
  const v = process.env[name] ?? dotenv[name];
  return v === undefined || v === null ? fallback : String(v).trim();
}

function envInt(name, fallback) {
  const n = Number.parseInt(env(name, ''), 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function envList(name) {
  return env(name, '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

export const config = {
  root: ROOT,
  appId: env('FEISHU_APP_ID'),
  appSecret: env('FEISHU_APP_SECRET'),
  agentBase: env('AGENT_BASE', 'http://127.0.0.1:8100').replace(/\/+$/, ''),
  allowedChatIds: envList('BOT_ALLOWED_CHAT_IDS'),
  allowedUserIds: envList('BOT_ALLOWED_USER_IDS'),
  rateLimitPerMin: envInt('BOT_RATE_LIMIT_PER_MIN', 10),
  maxReplyChars: envInt('BOT_MAX_REPLY_CHARS', 3800),
  agentTimeoutMs: envInt('AGENT_TIMEOUT_MS', 240000),
  logLevel: env('LOG_LEVEL', 'info'),
};

export function assertLiveConfig(cfg = config) {
  const missing = [];
  if (!cfg.appId) missing.push('FEISHU_APP_ID');
  if (!cfg.appSecret) missing.push('FEISHU_APP_SECRET');
  if (missing.length) {
    throw new Error(
      `缺少飞书应用配置：${missing.join(', ')}。请复制 .env.example 为 .env 并填写（不要提交仓库）。`,
    );
  }
}

export function assertAllowed(cfg, { chatId, userId }) {
  if (cfg.allowedChatIds.length && !cfg.allowedChatIds.includes(chatId)) {
    return `当前群未在白名单内（BOT_ALLOWED_CHAT_IDS），已拒绝。群 chat_id：${chatId}`;
  }
  if (cfg.allowedUserIds.length && !cfg.allowedUserIds.includes(userId)) {
    return '当前用户未在白名单内（BOT_ALLOWED_USER_IDS），已拒绝。';
  }
  return null;
}
