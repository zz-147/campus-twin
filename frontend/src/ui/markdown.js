/**
 * 极简 Markdown 渲染器（无第三方依赖）。
 *
 * 只覆盖 AI 运维回答里实际出现的语法：标题、段落、加粗/斜体、行内代码、
 * 无序/有序列表、引用、表格、围栏代码块、分割线、链接。
 *
 * 安全策略：先把原文整体 HTML 转义，再生成受控标签，因此后端/模型返回的
 * 任何 HTML 都不会被当作标签执行（防 XSS）。
 */
import { escapeHtml } from './format.js';

const BLOCK_START_RE =
  /^(#{1,6}\s|\s*```|\s*>|\s*([-*+]|\d+[.)])\s+|\s*([-*_])\3{2,}\s*$|\s*\|?\s*:?-{2,}:?\s*(\|.*)?$)/;

function isBlockStart(line) {
  return BLOCK_START_RE.test(line);
}

/** 行内语法：行内代码 > 加粗 > 斜体 > 链接（输入已转义） */
function renderInline(text) {
  let output = escapeHtml(text);
  const codeTokens = [];
  // 行内代码先抽出来占位，避免内部 * / _ 被当作强调语法
  output = output.replace(/`([^`]+)`/g, (_match, code) => {
    codeTokens.push(code);
    return `\u0000CODE${codeTokens.length - 1}\u0000`;
  });
  output = output.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  output = output.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  output = output.replace(/(^|[\s(（])\*([^*\n]+)\*(?=[\s).,;:!?，。；：！？）]|$)/g, '$1<em>$2</em>');
  output = output.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label, url) => {
    const safeUrl = /^https?:\/\//i.test(url) ? escapeHtml(url) : '#';
    return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
  output = output.replace(/\u0000CODE(\d+)\u0000/g, (_match, index) => {
    return `<code>${codeTokens[Number(index)] ?? ''}</code>`;
  });
  return output;
}

function splitTableRow(line) {
  let text = String(line).trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  return text.split('|').map((cell) => cell.trim());
}

function isTableSeparator(line) {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell));
}

/**
 * 渲染 Markdown → 安全的 HTML 字符串。
 * @param {string} source
 * @returns {string}
 */
export function renderMarkdown(source) {
  if (!source || typeof source !== 'string') return '';
  const lines = source.replace(/\r\n?/g, '\n').replace(/\u0000/g, '').split('\n');
  const html = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    // ---- 围栏代码块 ----
    const fence = line.match(/^\s*```+\s*([\w+#.-]*)\s*$/);
    if (fence) {
      const language = fence[1] || '';
      const buffer = [];
      index += 1;
      while (index < lines.length && !/^\s*```+\s*$/.test(lines[index])) {
        buffer.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1; // 跳过收尾 ```
      html.push(
        `<pre class="md-code"><code${
          language ? ` class="language-${escapeHtml(language)}"` : ''
        }>${escapeHtml(buffer.join('\n'))}</code></pre>`,
      );
      continue;
    }

    // ---- 空行 ----
    if (!line.trim()) {
      index += 1;
      continue;
    }

    // ---- 标题 ----
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 6);
      html.push(
        `<h${level} class="md-h md-h${level}">${renderInline(heading[2].trim())}</h${level}>`,
      );
      index += 1;
      continue;
    }

    // ---- 分割线 ----
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      html.push('<hr class="md-hr" />');
      index += 1;
      continue;
    }

    // ---- 表格（表头 + 分隔行） ----
    if (
      line.includes('|') &&
      index + 1 < lines.length &&
      lines[index + 1].includes('|') &&
      isTableSeparator(lines[index + 1])
    ) {
      const headers = splitTableRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      const headHtml = headers.map((cell) => `<th>${renderInline(cell)}</th>`).join('');
      const bodyHtml = rows
        .map(
          (row) =>
            `<tr>${headers
              .map((_header, cellIndex) => `<td>${renderInline(row[cellIndex] ?? '')}</td>`)
              .join('')}</tr>`,
        )
        .join('');
      html.push(
        `<div class="md-table-wrap"><table class="md-table"><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`,
      );
      continue;
    }

    // ---- 引用 ----
    if (/^>\s?/.test(line)) {
      const buffer = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        buffer.push(lines[index].replace(/^>\s?/, ''));
        index += 1;
      }
      html.push(`<blockquote class="md-quote">${buffer.map(renderInline).join('<br />')}</blockquote>`);
      continue;
    }

    // ---- 列表（支持缩进续行） ----
    const listItem = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (listItem) {
      const ordered = /^\d/.test(listItem[2]);
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!item) {
          // 缩进的非列表行视为上一条目的续行（AI 回答里常见）
          if (/^\s{2,}\S/.test(lines[index]) && items.length) {
            items[items.length - 1].push(lines[index].trim());
            index += 1;
            continue;
          }
          break;
        }
        if (/^\d/.test(item[2]) !== ordered) break;
        items.push([item[3]]);
        index += 1;
      }
      const tag = ordered ? 'ol' : 'ul';
      const itemsHtml = items
        .map((parts) => `<li>${parts.map(renderInline).join('<br />')}</li>`)
        .join('');
      html.push(`<${tag} class="md-list">${itemsHtml}</${tag}>`);
      continue;
    }

    // ---- 段落：合并连续普通行 ----
    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    html.push(`<p>${paragraph.map((part) => renderInline(part.trim())).join('<br />')}</p>`);
  }

  return html.join('\n');
}
