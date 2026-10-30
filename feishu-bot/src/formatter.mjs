// -*- mode: js -*-
/** 回复格式化：Markdown → 飞书可读纯文本、长文分条、追加来源标注。 */

export function stripMarkdown(text) {
  return String(text || '')
    .replace(/^#{1,6}\s*/gm, '')                 // 标题
    .replace(/```[\s\S]*?```/g, (m) => m.replace(/```[a-zA-Z]*\n?/g, '').trim()) // 代码块
    .replace(/`([^`]+)`/g, '$1')                 // 行内代码
    .replace(/\*\*([^*]+)\*\*/g, '$1')           // 粗体
    .replace(/\*([^*]+)\*/g, '$1')               // 斜体
    .replace(/^\s*[-*]\s+/gm, '· ')              // 列表
    .replace(/^\s*\|.*\|\s*$/gm, '')             // 表格行（简单丢弃，正文已含结论）
    .replace(/^\s*[-|:\s]+$/gm, '')              // 表格分隔线
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export function splitText(text, maxChars) {
  const clean = String(text || '').trim();
  if (!clean) return [];
  if (clean.length <= maxChars) return [clean];
  const chunks = [];
  let rest = clean;
  while (rest.length > maxChars) {
    let cut = rest.lastIndexOf('\n', maxChars);
    if (cut < maxChars * 0.5) cut = maxChars;
    chunks.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
  if (rest) chunks.push(rest);
  return chunks;
}

export function buildReply({ result, routed, footer = '' }) {
  const parts = [];
  if (result.ok) {
    parts.push(stripMarkdown(result.answer));
    const notice = routed?.needsConfirm || (result.actions || []).length
      ? '\n\n⚠ 写操作需人工确认：请到 Web 大屏「AI 面板」点确认执行，机器人不会自动执行。'
      : '';
    if (notice) parts.push(notice);
  } else {
    parts.push(`⚠ 暂时无法回答：${result.error}`);
    parts.push('\n你可以稍后重试，或在 Web 大屏使用 AI 面板。');
  }
  if (footer) parts.push(`\n${footer}`);
  return parts.join('').trim();
}
