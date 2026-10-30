/**
 * 「发现新设备」提示条（W9 自动发现 → 用户确认/撤销）。
 *
 * 数据来自 GET /api/scene/pending（或场景配置里 status=pending 的设备）：
 *  - 有推断楼栋：显示“推断楼栋：教学楼”，可直接确认加入 / 撤销；
 *  - 无推断楼栋：提供楼栋下拉，选择后加入；
 *  - 提示条位于顶部状态栏下方、告警横幅同层，不遮挡 3D 场景操作。
 */
import { escapeHtml } from './format.js';

export class NewDevicesBanner {
  constructor(root = document, handlers = {}) {
    this.root = root;
    this.handlers = handlers;
    this.el = {
      banner: root.getElementById('new-device-banner'),
      count: root.getElementById('new-device-count'),
      list: root.getElementById('new-device-list'),
      collapse: root.getElementById('new-device-collapse'),
    };
    this._collapsed = false;
    this._bindEvents();
  }

  _bindEvents() {
    this.el.collapse?.addEventListener('click', () => {
      this._collapsed = !this._collapsed;
      this.el.banner?.classList.toggle('collapsed', this._collapsed);
      if (this.el.collapse) this.el.collapse.textContent = this._collapsed ? '展开' : '收起';
    });
    this.el.list?.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const row = button.closest('.new-device-item');
      const name = row?.dataset.name || '';
      const action = button.dataset.action;
      if (action === 'confirm') {
        const select = row?.querySelector('select[data-role="building-select"]');
        this.handlers.onConfirm?.({ name, buildingId: select ? select.value : '' });
      } else if (action === 'remove') {
        this.handlers.onRemove?.({ name });
      } else if (action === 'config') {
        this.handlers.onViewConfig?.({ name });
      }
    });
  }

  /**
   * @param {Array} pendingDevices
   * @param {Array} buildings 场景配置楼栋（用于下拉选择）
   */
  update(pendingDevices = [], buildings = []) {
    const banner = this.el.banner;
    if (!banner) return;
    const pending = Array.isArray(pendingDevices) ? pendingDevices : [];
    if (!pending.length) {
      banner.classList.add('hidden');
      this.pending = [];
      return;
    }
    this.pending = pending;
    pending.forEach((device) => {
      if (!device.inferredBuilding) {
        const matched = buildings.find(
          (building) =>
            building.id === device.buildingId ||
            building.name === device.buildingId ||
            building.name === device.inferredBuilding,
        );
        if (matched) device.inferredBuilding = matched.name;
      }
    });
    if (this.el.count) this.el.count.textContent = String(pending.length);
    if (this.el.list) {
      this.el.list.innerHTML = pending
        .map((device) => {
          const inferred =
            device.inferredBuilding ||
            (buildings.find((building) => building.id === device.buildingId)?.name ?? '');
          const inferredName = buildings.find((building) => building.id === device.buildingId)?.name;
          const inferredText = inferred || inferredName || '';
          const buildingOptions = buildings
            .map(
              (building) =>
                `<option value="${escapeHtml(building.id)}">${escapeHtml(building.name)}</option>`,
            )
            .join('');
          const inferredLine = inferredText
            ? `<span class="new-device-inferred">推断楼栋：${escapeHtml(inferredText)}</span>`
            : `<span class="new-device-inferred warn">未推断出楼栋，请选择
                 <select class="new-device-select" data-role="building-select">${buildingOptions}</select>
               </span>`;
          const meta = [device.model, device.ip, device.type].filter(Boolean).join(' · ');
          return `<div class="new-device-item" data-name="${escapeHtml(device.name)}">
            <div class="new-device-info">
              <span class="new-device-name">${escapeHtml(device.name)}</span>
              <span class="new-device-badge">新设备</span>
              ${meta ? `<span class="new-device-meta">${escapeHtml(meta)}</span>` : ''}
              ${inferredLine}
            </div>
            <div class="new-device-actions">
              <button type="button" class="btn small" data-action="confirm">确认加入</button>
              <button type="button" class="btn small ghost" data-action="remove">撤销</button>
              <button type="button" class="btn small ghost" data-action="config" title="查看场景配置">配置</button>
            </div>
          </div>`;
        })
        .join('');
    }
    banner.classList.remove('hidden');
  }
}
