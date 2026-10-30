/**
 * 通用确认弹窗 / 选择弹窗（W9 危险操作二次确认）。
 * 用 Promise 返回用户选择，避免每个功能各写一套 modal。
 */

function ensureOverlay() {
  let overlay = document.getElementById('confirm-overlay');
  if (overlay) return overlay;
  overlay = document.createElement('div');
  overlay.id = 'confirm-overlay';
  overlay.className = 'confirm-overlay hidden';
  overlay.innerHTML = `
    <div class="confirm-dialog" role="dialog" aria-modal="true">
      <div class="confirm-title" id="confirm-title">确认操作</div>
      <div class="confirm-message" id="confirm-message"></div>
      <div class="confirm-extra" id="confirm-extra"></div>
      <div class="confirm-actions">
        <button type="button" class="btn ghost" id="confirm-cancel">取消</button>
        <button type="button" class="btn danger" id="confirm-ok">确认</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  return overlay;
}

/**
 * 通用确认框。
 * @param {object} options
 * @param {string} options.title
 * @param {string} options.message
 * @param {string} options.confirmText
 * @param {boolean} options.danger 危险操作（红色确认按钮）
 * @param {Array<{label:string,value:string}>} [options.options] 下拉选项（可选）
 * @returns {Promise<boolean|string|null>} true/false；带 options 时返回选中的 value 或 null
 */
export function confirmDialog({
  title = '确认操作',
  message = '',
  confirmText = '确认',
  danger = false,
  options = null,
  cancelText = '取消',
} = {}) {
  const overlay = ensureOverlay();
  const titleEl = overlay.querySelector('#confirm-title');
  const messageEl = overlay.querySelector('#confirm-message');
  const extraEl = overlay.querySelector('#confirm-extra');
  const okBtn = overlay.querySelector('#confirm-ok');
  const cancelBtn = overlay.querySelector('#confirm-cancel');

  titleEl.textContent = title;
  messageEl.textContent = message;
  okBtn.textContent = confirmText;
  okBtn.classList.toggle('danger', Boolean(danger));
  cancelBtn.textContent = cancelText;

  let select = null;
  extraEl.innerHTML = '';
  if (Array.isArray(options) && options.length) {
    select = document.createElement('select');
    select.className = 'confirm-select';
    select.innerHTML = options
      .map((option) => `<option value="${String(option.value)}">${String(option.label)}</option>`)
      .join('');
    extraEl.appendChild(select);
  }

  return new Promise((resolve) => {
    const cleanup = (value) => {
      overlay.classList.add('hidden');
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      overlay.removeEventListener('click', onOverlayClick);
      document.removeEventListener('keydown', onKeyDown);
      resolve(value);
    };
    const onOk = () => cleanup(select ? select.value : true);
    const onCancel = () => cleanup(select ? null : false);
    const onOverlayClick = (event) => {
      if (event.target === overlay) cleanup(select ? null : false);
    };
    const onKeyDown = (event) => {
      if (event.key === 'Escape') cleanup(select ? null : false);
    };
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onOverlayClick);
    document.addEventListener('keydown', onKeyDown);
    overlay.classList.remove('hidden');
    okBtn.focus();
  });
}
