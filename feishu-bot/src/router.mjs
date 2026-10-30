// -*- mode: js -*-
/** 指令路由：飞书消息 → Agent 问题；写操作只提示确认，绝不由机器人执行。 */

export const HELP_TEXT = [
  '【校园网络数字孪生平台 · 飞书助手】',
  '',
  '直接发消息即可，例如：',
  '· 状态 —— 查看整体网络健康度',
  '· 告警 —— 查看告警与根因分组',
  '· 路径 PC-JX1 到 Server-1 —— 查转发路径',
  '· 读 configs/core-3650.txt —— 通过 MCP 读真实配置文件',
  '· 其他问题直接问（AI 会调用真实网络数据）',
  '',
  '安全：删除/清空类写操作只会生成「待确认」提示，必须在 Web 大屏点确认后才会执行。',
].join('\n');

const STATUS_WORDS = /^(状态|整体状态|网络状态|健康|健康度|网络健康|看看网络|网络怎么样)[?？!！。]*$/;
const ALERT_WORDS = /^(告警|告警状态|告警情况|故障|根因|告警根因|有哪些告警)[?？!！。]*$/;
const PATH_RE = /^(?:路径|路由|追踪)\s*(.+?)\s*(?:到|->|→|至)\s*(.+?)[?？!！。]*$/;
const READ_RE = /^(?:读|读取|打开|查看)\s+(?:文件\s*)?([^\s，,。]+\.(?:txt|md|json|yaml|yml|conf|cfg))[?？!！。]*$/;
const WRITE_CLEAR_RE = /(?:(?:清空|删除|清理|移除|清除).{0,8}(?:告警|历史|记录|对话|会话)|(?:告警|历史|记录|对话|会话).{0,8}(?:清空|删除|清理|移除|清除))/;
const WRITE_SILENCE_RE = /(?:(?:静默|忽略|屏蔽).{0,8}告警|告警.{0,8}(?:静默|忽略|屏蔽))/;

/** @returns {{kind:string, question?:string, reply?:string, needsConfirm?:boolean}} */
export function routeMessage(rawText) {
  const text = String(rawText || '').trim();
  if (!text) return { kind: 'empty', reply: '请输入问题，例如「状态」「告警」「路径 PC-JX1 到 Server-1」。' };
  if (/^(\/help|帮助|help|菜单|指令|命令)$/i.test(text)) return { kind: 'help', reply: HELP_TEXT };

  const path = text.match(PATH_RE);
  if (path) {
    return { kind: 'path', question: `${path[1].trim()} 到 ${path[2].trim()} 的路径怎么走？` };
  }
  const read = text.match(READ_RE);
  if (read) {
    return { kind: 'read_file', question: `帮我读一下 ${read[1].trim()}` };
  }
  if (WRITE_CLEAR_RE.test(text)) {
    return { kind: 'write', needsConfirm: true, question: text };
  }
  if (WRITE_SILENCE_RE.test(text)) {
    return { kind: 'write', needsConfirm: true, question: `静默告警：${text}` };
  }
  if (STATUS_WORDS.test(text)) return { kind: 'status', question: '现在整体网络状态怎么样？' };
  if (ALERT_WORDS.test(text)) return { kind: 'alerts', question: '现在有哪些告警，根因是什么？' };
  return { kind: 'chat', question: text };
}

export function formatActionNotice(actions) {
  if (!Array.isArray(actions) || !actions.length) return '';
  const lines = ['', '⚠ 该请求包含写操作，需要你确认：'];
  for (const action of actions) {
    lines.push(`· ${action.confirm_text || action.label || action.type || '待确认操作'}`);
  }
  lines.push('请打开 Web 大屏（http://127.0.0.1:5173）在 AI 面板点「确认执行」，机器人不会自动执行写操作。');
  return lines.join('\n');
}
