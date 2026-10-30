/**
 * 文字标签工具：用 CanvasTexture 现场绘制中文标签 Sprite。
 * 不依赖任何外部 CDN 字体（使用系统字体栈：微软雅黑 / 苹方 / Noto Sans SC）。
 */
import * as THREE from 'three';

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

/**
 * @param {string} text 标签文字
 * @param {object} options
 * @param {number} options.worldHeight 标签在 3D 世界中的高度（宽度按比例自适应）
 * @param {number} options.fontSize 画布字号（像素）
 * @param {string} options.color 文字颜色
 * @param {string} options.accent 状态圆点 / 边框颜色
 * @param {string} options.background 背景色
 */
export function makeLabelSprite(text, options = {}) {
  const {
    worldHeight = 2.9,
    fontSize = 34,
    color = '#e8f6ff',
    accent = '#4cc9f0',
    background = 'rgba(6, 17, 30, 0.72)',
    bold = true,
    showDot = true,
  } = options;
  const label = String(text ?? '').slice(0, 28);
  const dpr = 3;

  const paddingX = 16;
  const dotSize = showDot ? 10 : 0;
  const gap = showDot ? 12 : 0;
  const font = `${bold ? '600 ' : ''}${fontSize}px "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif`;

  // 先测量文本宽度，再决定画布尺寸
  const measureCtx = document.createElement('canvas').getContext('2d');
  measureCtx.font = font;
  const textWidth = Math.max(measureCtx.measureText(label).width, fontSize * 0.9);
  const width = Math.ceil(paddingX * 2 + dotSize + gap + textWidth);
  const height = Math.ceil(fontSize + 20);

  const canvas = document.createElement('canvas');
  canvas.width = Math.ceil(width * dpr);
  canvas.height = Math.ceil(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  // 半透明圆角底板 + 状态色描边
  roundRect(ctx, 0.5, 0.5, width - 1, height - 1, height * 0.32);
  ctx.fillStyle = background;
  ctx.fill();
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.2;
  ctx.stroke();

  let cursorX = paddingX;
  if (showDot) {
    ctx.beginPath();
    ctx.arc(cursorX + dotSize / 2, height / 2, dotSize / 2, 0, Math.PI * 2);
    ctx.fillStyle = accent;
    ctx.fill();
    cursorX += dotSize + gap;
  }

  ctx.font = font;
  ctx.fillStyle = color;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(label, cursorX, height / 2 + 1);

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.generateMipmaps = false;

  const material = new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    depthWrite: false,
    depthTest: true,
  });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(worldHeight * (width / height), worldHeight, 1);
  sprite.renderOrder = 10;
  sprite.userData.isLabel = true;
  sprite.userData.text = label;
  sprite.raycast = () => {}; // 文字标签不参与鼠标拾取，避免挡住设备点击
  return sprite;
}

/** 释放标签占用的贴图 / 材质（设备标记重建时调用） */
export function disposeLabelSprite(sprite) {
  if (!sprite) return;
  sprite.material?.map?.dispose();
  sprite.material?.dispose();
}
