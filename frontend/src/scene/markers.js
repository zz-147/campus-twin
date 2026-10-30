/**
 * 设备 / 主机标记几何体工厂。
 *
 * 形状约定：
 *   交换机=小盒子 · 路由器=圆柱 · PC=小方块 · 服务器=机架条
 *   无线 AP=锥形天线 · 摄像头=小盒子 · NC=发光八面体
 * 颜色约定（状态四分类）：
 *   已纳管=绿 / 不可达=红 / 不支持=灰 / 无数据=蓝灰
 *
 * W9：`pending` 新设备使用浅色半透明 + 黄色虚线外框 + 「新设备」角标；
 * 确认后自动恢复正常样式。
 */
import * as THREE from 'three';
import { STATUS_META } from '../api/normalize.js';
import { disposeLabelSprite, makeLabelSprite } from './labels.js';

/** 同类设备共享几何体：减少内存占用与 draw call 准备开销 */
const SHARED_GEOMETRY = {
  switch: new THREE.BoxGeometry(4.6, 2.0, 3.6),
  router: new THREE.CylinderGeometry(2.0, 2.25, 3.2, 20),
  pc: new THREE.BoxGeometry(2.7, 2.7, 2.7),
  serverBar: new THREE.BoxGeometry(3.6, 0.8, 2.6),
  ap: new THREE.ConeGeometry(1.9, 3.6, 10),
  camera: new THREE.BoxGeometry(2.6, 1.8, 1.8),
  unknown: new THREE.BoxGeometry(2.9, 2.9, 2.9),
  controller: new THREE.OctahedronGeometry(3.0, 0),
  controllerRing: new THREE.TorusGeometry(4.2, 0.11, 8, 48),
  pendingOutline: new THREE.BoxGeometry(6.2, 5.2, 4.8),
};

function createStatusMaterial(color) {
  return new THREE.MeshStandardMaterial({
    color,
    emissive: color,
    emissiveIntensity: 0.32,
    metalness: 0.55,
    roughness: 0.35,
  });
}

function createPendingOutline() {
  const edges = new THREE.EdgesGeometry(SHARED_GEOMETRY.pendingOutline);
  const material = new THREE.LineDashedMaterial({
    color: 0xffc857,
    dashSize: 0.9,
    gapSize: 0.6,
    transparent: true,
    opacity: 0.85,
  });
  const outline = new THREE.LineSegments(edges, material);
  outline.computeLineDistances();
  outline.position.y = 0.8;
  outline.userData.isPendingOutline = true;
  outline.raycast = () => {};
  return outline;
}

function createPendingBadge() {
  const sprite = makeLabelSprite('新设备', {
    worldHeight: 2.6,
    fontSize: 28,
    color: '#1b1405',
    accent: '#ffc857',
    background: 'rgba(255,200,87,0.92)',
    showDot: false,
  });
  sprite.userData.isPendingBadge = true;
  return sprite;
}

/**
 * 创建设备标记（Group：几何体 + 文字标签 +（pending 时）虚线框/角标）。
 * @param {object} item buildSceneItems() 的条目
 */
export function createMarker(item) {
  const group = new THREE.Group();
  group.position.set(item.position.x, item.position.y, item.position.z);
  group.userData = { key: item.key, kind: 'marker' };

  const statusMeta = item.statusMeta || STATUS_META.unknown;
  const material = createStatusMaterial(statusMeta.color);
  const meshes = [];

  switch (item.type) {
    case 'switch': {
      const mesh = new THREE.Mesh(SHARED_GEOMETRY.switch, material);
      group.add(mesh);
      meshes.push(mesh);
      break;
    }
    case 'router': {
      const mesh = new THREE.Mesh(SHARED_GEOMETRY.router, material);
      group.add(mesh);
      meshes.push(mesh);
      break;
    }
    case 'pc': {
      const mesh = new THREE.Mesh(SHARED_GEOMETRY.pc, material);
      group.add(mesh);
      meshes.push(mesh);
      break;
    }
    case 'server': {
      for (let index = 0; index < 3; index += 1) {
        const mesh = new THREE.Mesh(SHARED_GEOMETRY.serverBar, material);
        mesh.position.y = -1.0 + index * 0.95;
        group.add(mesh);
        meshes.push(mesh);
      }
      break;
    }
    case 'ap': {
      const mesh = new THREE.Mesh(SHARED_GEOMETRY.ap, material);
      mesh.position.y = 0.4;
      group.add(mesh);
      meshes.push(mesh);
      break;
    }
    case 'camera': {
      const mesh = new THREE.Mesh(SHARED_GEOMETRY.camera, material);
      group.add(mesh);
      meshes.push(mesh);
      break;
    }
    case 'controller': {
      const mesh = new THREE.Mesh(SHARED_GEOMETRY.controller, material);
      group.add(mesh);
      meshes.push(mesh);
      const ring = new THREE.Mesh(
        SHARED_GEOMETRY.controllerRing,
        new THREE.MeshBasicMaterial({
          color: 0x54e6ff,
          transparent: true,
          opacity: 0.6,
          depthWrite: false,
        }),
      );
      ring.rotation.x = Math.PI / 2;
      ring.userData.isControllerRing = true;
      group.add(ring);
      break;
    }
    default: {
      const mesh = new THREE.Mesh(SHARED_GEOMETRY.unknown, material);
      group.add(mesh);
      meshes.push(mesh);
    }
  }

  for (const mesh of meshes) {
    mesh.userData.key = item.key;
    mesh.userData.isMarker = true;
    mesh.castShadow = false;
    mesh.receiveShadow = false;
  }

  const label = makeLabelSprite(item.label || item.hostname, {
    worldHeight: 4.2,
    fontSize: 32,
    color: '#e8f6ff',
    accent: statusMeta.css,
    showDot: false,
  });
  label.position.set(0, item.labelOffsetY ?? 4.6, 0);
  group.add(label);

  const marker = {
    key: item.key,
    group,
    meshes,
    material,
    label,
    item,
    phase: Math.random() * Math.PI * 2,
  };
  setMarkerPending(marker, Boolean(item.pending));
  return marker;
}

/** 切换 pending 视觉：浅色半透明 + 虚线框 + 「新设备」角标 */
export function setMarkerPending(marker, pending) {
  if (!marker) return;
  marker.pending = Boolean(pending);
  marker.material.transparent = marker.pending;
  marker.material.opacity = marker.pending ? 0.55 : 1;
  marker.material.emissiveIntensity = marker.pending ? 0.18 : 0.32;

  let outline = marker.group.children.find((child) => child.userData?.isPendingOutline);
  if (marker.pending && !outline) {
    outline = createPendingOutline();
    marker.group.add(outline);
  } else if (outline) {
    outline.visible = marker.pending;
  }

  let badge = marker.group.children.find((child) => child.userData?.isPendingBadge);
  if (marker.pending && !badge) {
    badge = createPendingBadge();
    badge.position.set(0, (marker.item?.labelOffsetY ?? 4.6) + 4.2, 0);
    marker.group.add(badge);
  } else if (badge) {
    badge.visible = marker.pending;
  }
  marker.group.userData.pending = marker.pending;
}

/** 数据刷新时原地更新标记颜色 / 告警状态 / pending 状态（不重建对象，保证性能与选中状态稳定） */
export function updateMarker(marker, item) {
  marker.item = item;
  const statusMeta = item.statusMeta || STATUS_META.unknown;
  marker.material.color.setHex(statusMeta.color);
  marker.material.emissive.setHex(statusMeta.color);
  if (marker.pending !== Boolean(item.pending)) setMarkerPending(marker, Boolean(item.pending));
  marker.material.emissiveIntensity = marker.pending ? 0.18 : item.alert ? 0.6 : 0.32;
  marker.group.userData.alert = item.alert;
}

/** 释放标记资源（仅释放在本模块内创建的材质/标签；共享几何体保留） */
export function disposeMarker(marker) {
  if (!marker) return;
  disposeLabelSprite(marker.label);
  marker.material?.dispose();
  for (const child of marker.group.children) {
    if (child.userData?.isControllerRing) child.material?.dispose();
    if (child.userData?.isPendingOutline) {
      child.geometry?.dispose();
      child.material?.dispose();
    }
    if (child.userData?.isPendingBadge) disposeLabelSprite(child);
  }
}

export { SHARED_GEOMETRY };
