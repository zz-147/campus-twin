/** UI 格式化工具（时间 / 时长 / 百分比 / HTML 转义）。 */

/** 秒数 → 中文时长（<60s 显示 x.x 秒，否则 x 分 y 秒） */
export function formatDuration(seconds) {
  const value = Number(seconds) || 0;
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)} 秒`;
  return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
}

const HTML_ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/** 转义用户/后端文本，避免 innerHTML 注入 */
export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

/** 把后端时间（epoch 秒/毫秒、ISO、PT 本地时间字符串）统一转成 Date */
export function toDate(value) {
  if (value === null || value === undefined || value === '') return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return new Date(value > 1e12 ? value : value * 1000);
  }
  const text = String(value).trim();
  if (!text) return null;
  const numeric = Number(text);
  if (Number.isFinite(numeric)) return new Date(numeric > 1e12 ? numeric : numeric * 1000);
  const parsed = Date.parse(text.replace(' ', 'T'));
  return Number.isNaN(parsed) ? null : new Date(parsed);
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

/** HH:mm:ss（本地时区） */
export function formatClock(value) {
  const date = toDate(value);
  if (!date) return '—';
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

/** MM-DD HH:mm:ss（本地时区） */
export function formatDateTime(value) {
  const date = toDate(value);
  if (!date) return '—';
  return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(
    date.getMinutes(),
  )}:${pad2(date.getSeconds())}`;
}

/** 数值 → 百分比文本（如 88.4%） */
export function formatPercent(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${Number(value).toFixed(digits)}%`;
}

export function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}
