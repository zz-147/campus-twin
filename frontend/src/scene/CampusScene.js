/**
 * 校园数字孪生 3D 场景（Three.js）。
 *
 * 层级结构：
 *   scene
 *   ├─ 地面 / 网格 / 主干道 / 灯光
 *   ├─ buildingRoot：楼栋（半透明 Box + 合并棱线 + 中文名 Sprite），支持配置驱动动态重建
 *   ├─ markerRoot：设备 / 主机 / NC 标记（每台 1 组：几何体 + 文字标签）
 *   ├─ linkRoot：拓扑链路（每链路 1 条 Line）
 *   ├─ haloMesh：所有告警光环共用一个 InstancedMesh
 *   └─ selectionRing：选中的圆环（全局复用 1 个）
 *
 * W9：楼栋/设备来自后端配置（GET /api/scene），支持运行时新增楼栋/设备；
 * 新设备出现后自动高亮 3 秒。交互：OrbitControls 旋转/缩放/平移，点击设备聚焦并回调 UI。
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { BUILDINGS } from './layout.js';
import { buildingSignature } from './sceneConfig.js';
import { makeLabelSprite } from './labels.js';
import { disposeLabelSprite } from './labels.js';
import { createMarker, disposeMarker, updateMarker } from './markers.js';

// 初始相机视角（4 栋默认楼栋时）；加载配置后会根据楼栋包围盒自动调整
const INITIAL_CAMERA = new THREE.Vector3(44, 166, 246);
const INITIAL_TARGET = new THREE.Vector3(44, 0, -44);
const CAMERA_START = new THREE.Vector3(44, 400, 540);
const MAX_HALOS = 32; // 告警光环实例上限（当前规模远小于此值）
const HIGHLIGHT_MS = 3000; // 新设备/新楼栋高亮时长

function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** 读取配置里的颜色（#rrggbb / 0xrrggbb / 数字） */
function styleColor(value, fallback) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const text = String(value || '').trim();
  if (/^#[0-9a-f]{6}$/i.test(text)) return parseInt(text.slice(1), 16);
  if (/^0x[0-9a-f]{1,6}$/i.test(text)) return parseInt(text.slice(2), 16);
  return fallback;
}

export class CampusScene {
  constructor(container, handlers = {}) {
    if (!container) throw new Error('缺少 3D 场景容器 #scene-root');
    this.container = container;
    this.onSelect = handlers.onSelect || (() => {});
    this.onDeselect = handlers.onDeselect || (() => {});
    this.onHover = handlers.onHover || (() => {});

    this.autoRotate = true;
    this._selectedKey = null;
    this._cameraTween = null;
    this._markers = new Map();
    this._items = [];
    this._itemsByKey = new Map();
    this._alertMarkers = [];
    this._pickMeshes = [];
    // W9：配置驱动楼栋（初始为默认 4 栋，加载 /api/scene 后原位重建）
    this._buildings = BUILDINGS.map((building) => ({
      ...building,
      status: 'confirmed',
      style: {},
      devices: [],
    }));
    this._buildingSignature = buildingSignature(this._buildings);
    this._campusCenter = { x: 0, z: -42 };
    this._defaultCamera = INITIAL_CAMERA.clone();
    this._defaultTarget = INITIAL_TARGET.clone();
    this._buildingHighlightUntil = 0;
    this._buildingBodies = [];
    this._sceneLoadedFromConfig = false;
    this._userInteracted = false;
    this._hasAppliedItems = false;
    // W10：路径追踪可视化（发光路径 + hop 编号 + 断点 ✕）
    this._pathRoot = null;
    this._pathTrace = null;
    this._pathHops = [];
    this._pathBreakpoint = null;
    this._pathCurve = null;
    this._pathPulses = [];
    this._pathStats = { active: false, hops: 0, breakpoint: false };

    this._initRenderer();
    this._initScene();
    this._initCamera();
    this._initLights();
    this._initGround();
    this._initBuildings();
    this._initGroups();
    this._initInteractions();
    this._initResize();

    this._clock = new THREE.Clock();
    this._startIntro();
    this.renderer.setAnimationLoop(() => this._animate());
  }

  // ------------------------------------------------------------------ //
  // 初始化
  // ------------------------------------------------------------------ //
  _initRenderer() {
    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: false,
      powerPreference: 'high-performance',
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.22;
    this.domElement = this.renderer.domElement;
    this.domElement.style.display = 'block';
    this.container.appendChild(this.domElement);
    // 初始尺寸直接取容器大小（ResizeObserver 在 _initResize 中接管后续变化）
    this.renderer.setSize(
      this.container.clientWidth || window.innerWidth,
      this.container.clientHeight || window.innerHeight,
    );
  }

  _initScene() {
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x040a14);
    this.scene.fog = new THREE.Fog(0x040a14, 420, 950);
  }

  _initCamera() {
    const width = this.container.clientWidth || window.innerWidth;
    const height = this.container.clientHeight || window.innerHeight;
    this.camera = new THREE.PerspectiveCamera(52, width / Math.max(height, 1), 1, 2400);
    this.camera.position.copy(CAMERA_START);

    this.controls = new OrbitControls(this.camera, this.domElement);
    this.controls.target.copy(this._defaultTarget);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.07;
    this.controls.minDistance = 45;
    this.controls.maxDistance = 900;
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.controls.autoRotateSpeed = 0.55;
    this.controls.update();
    // 用户一旦手动操作相机，后续配置刷新不再自动拉回默认视角
    this.controls.addEventListener('start', () => {
      this._userInteracted = true;
    });
  }

  _initLights() {
    this.scene.add(new THREE.HemisphereLight(0xa8d8ff, 0x0a1626, 1.35));
    const keyLight = new THREE.DirectionalLight(0xd6ecff, 2.1);
    keyLight.position.set(180, 280, 160);
    this.scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight(0x3f7fbf, 0.65);
    fillLight.position.set(-220, 140, -180);
    this.scene.add(fillLight);
  }

  _initGround() {
    // 地面
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(1200, 1000),
      new THREE.MeshStandardMaterial({ color: 0x0a1422, roughness: 0.95, metalness: 0.1 }),
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.2;
    this.scene.add(ground);

    // 网格（科技感参考线）
    const grid = new THREE.GridHelper(1200, 60, 0x24587f, 0x12314f);
    grid.position.y = 0.02;
    grid.material.transparent = true;
    grid.material.opacity = 0.35;
    this.scene.add(grid);

    // 主干道：中心楼 → 其余楼栋（配置变化时动态重建）
    this.roadRoot = new THREE.Group();
    this.roadRoot.name = 'roads';
    this.scene.add(this.roadRoot);
    this._rebuildRoads();
  }

  _initBuildings() {
    this.buildingRoot = new THREE.Group();
    this.buildingRoot.name = 'buildings';
    this.scene.add(this.buildingRoot);
    this._buildBuildings();
  }

  /** 按当前楼栋配置重建楼体（配置驱动的动态场景；位置/尺寸/名称/颜色变化时触发） */
  _buildBuildings() {
    // 释放上一批楼栋对象
    while (this.buildingRoot.children.length) {
      const child = this.buildingRoot.children.pop();
      this.buildingRoot.remove(child);
      if (child.userData?.isBuildingLabel) disposeLabelSprite(child);
      child.geometry?.dispose();
      child.material?.dispose();
    }
    this.buildingLabels = [];
    this._buildingBodies = [];
    const edgePositions = [];

    for (const building of this._buildings) {
      const bodyGeometry = new THREE.BoxGeometry(building.width, building.height, building.depth);
      const styleOpacity = Number(building.style?.opacity);
      const bodyOpacity = Number.isFinite(styleOpacity)
        ? Math.min(0.6, Math.max(0.08, styleOpacity))
        : 0.2;
      const body = new THREE.Mesh(
        bodyGeometry,
        new THREE.MeshStandardMaterial({
          color: styleColor(building.style?.color, building.color),
          emissive: styleColor(building.style?.color, building.color),
          emissiveIntensity: 0.08,
          transparent: true,
          opacity: bodyOpacity,
          roughness: 0.4,
          metalness: 0.45,
          depthWrite: false,
        }),
      );
      body.position.set(building.x, building.height / 2, building.z);
      body.userData.isBuilding = true;
      body.userData.buildingId = building.id;
      body.raycast = () => {}; // 半透明楼体不参与点击拾取
      this.buildingRoot.add(body);
      this._buildingBodies.push(body);

      const edges = new THREE.EdgesGeometry(bodyGeometry);
      const positions = edges.attributes.position;
      const appendVertex = (index) => {
        edgePositions.push(
          positions.getX(index) + building.x,
          positions.getY(index) + building.height / 2,
          positions.getZ(index) + building.z,
        );
      };
      if (edges.index) {
        for (let i = 0; i < edges.index.count; i += 1) appendVertex(edges.index.getX(i));
      } else {
        for (let i = 0; i < positions.count; i += 1) appendVertex(i);
      }
      edges.dispose();

      // 楼层横线（并入同一个 LineSegments，不增加对象数）
      const halfWidth = building.width / 2;
      const halfDepth = building.depth / 2;
      const corners = [
        [-halfWidth, -halfDepth],
        [halfWidth, -halfDepth],
        [halfWidth, halfDepth],
        [-halfWidth, halfDepth],
      ];
      for (const fraction of [0.34, 0.67]) {
        const floorY = building.height * fraction;
        for (let i = 0; i < corners.length; i += 1) {
          const from = corners[i];
          const to = corners[(i + 1) % corners.length];
          edgePositions.push(
            from[0] + building.x,
            floorY,
            from[1] + building.z,
            to[0] + building.x,
            floorY,
            to[1] + building.z,
          );
        }
      }

      // 中文楼栋名（CanvasTexture Sprite，不依赖外部字体）；
      // pending 新楼栋用 amber 描边，提示待确认
      const pendingBuilding = building.status === 'pending';
      const label = makeLabelSprite(pendingBuilding ? `${building.name}（待确认）` : building.name, {
        worldHeight: 10,
        fontSize: 48,
        color: pendingBuilding ? '#ffe1a1' : '#e2f4ff',
        accent: pendingBuilding ? '#ffc857' : '#4cc9f0',
        background: 'rgba(5, 14, 26, 0.86)',
        showDot: false,
      });
      label.userData.isBuildingLabel = true;
      label.position.set(building.x, building.height + 26, building.z);
      this.buildingLabels.push(label);
      this.buildingRoot.add(label);
    }

    const edgeGeometry = new THREE.BufferGeometry();
    edgeGeometry.setAttribute('position', new THREE.Float32BufferAttribute(edgePositions, 3));
    this.buildingRoot.add(
      new THREE.LineSegments(
        edgeGeometry,
        new THREE.LineBasicMaterial({ color: 0x6ab4e0, transparent: true, opacity: 0.7 }),
      ),
    );
    this._buildingSignature = buildingSignature(this._buildings);
  }

  /** 主干道：中心楼 → 其它楼栋（动态跟随楼栋配置） */
  _rebuildRoads() {
    while (this.roadRoot.children.length) {
      const child = this.roadRoot.children.pop();
      this.roadRoot.remove(child);
      child.geometry?.dispose();
      child.material?.dispose();
    }
    if (!this._buildings.length) return;
    const hub =
      this._buildings.find((building) => /机房|core/i.test(`${building.name} ${building.id}`)) ||
      this._buildings[0];
    const points = [];
    for (const building of this._buildings) {
      if (building.id === hub.id) continue;
      points.push(hub.x, 0.06, hub.z, building.x, 0.06, building.z);
    }
    if (!points.length) return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3));
    this.roadRoot.add(
      new THREE.LineSegments(
        geometry,
        new THREE.LineBasicMaterial({ color: 0x1f5f8c, transparent: true, opacity: 0.7 }),
      ),
    );
  }

  /**
   * 应用后端配置的楼栋集合（GET /api/scene）。
   * 楼栋数量/位置/尺寸/名称变化时重建楼体并调整默认视角；返回是否发生变化。
   */
  setBuildings(buildings = []) {
    if (!Array.isArray(buildings) || !buildings.length) return false;
    const nextSignature = buildingSignature(buildings);
    if (nextSignature === this._buildingSignature && this._sceneLoadedFromConfig) return false;
    const firstConfigLoad = !this._sceneLoadedFromConfig;
    this._buildings = buildings.map((building) => ({
      ...building,
      width: Math.max(12, Number(building.width) || 60),
      depth: Math.max(12, Number(building.depth) || 30),
      height: Math.max(8, Number(building.height) || 20),
    }));
    this._sceneLoadedFromConfig = true;
    this._buildBuildings();
    this._rebuildRoads();
    this._updateCampusBounds();
    if (firstConfigLoad || !this._userInteracted) {
      this._tweenCamera(this._defaultCamera, this._defaultTarget, firstConfigLoad ? 1200 : 900);
    }
    this._buildingHighlightUntil = firstConfigLoad ? 0 : performance.now() + HIGHLIGHT_MS;
    return true;
  }

  /** 根据楼栋包围盒计算默认相机视角（适配任意数量/位置的楼栋） */
  _updateCampusBounds() {
    if (!this._buildings.length) return;
    let minX = Infinity;
    let maxX = -Infinity;
    let minZ = Infinity;
    let maxZ = -Infinity;
    for (const building of this._buildings) {
      minX = Math.min(minX, building.x - building.width / 2);
      maxX = Math.max(maxX, building.x + building.width / 2);
      minZ = Math.min(minZ, building.z - building.depth / 2);
      maxZ = Math.max(maxZ, building.z + building.depth / 2);
    }
    const centerX = (minX + maxX) / 2;
    const centerZ = (minZ + maxZ) / 2;
    const span = Math.max(maxX - minX, maxZ - minZ, 260);
    const scale = Math.max(1, span / 320);
    this._campusCenter = { x: centerX, z: centerZ };
    this._defaultTarget = new THREE.Vector3(centerX + 44, 0, centerZ - 20 * scale);
    this._defaultCamera = new THREE.Vector3(
      this._defaultTarget.x,
      166 * scale,
      this._defaultTarget.z + 290 * scale,
    );
    // 相机远裁剪面随场景范围放大
    this.camera.far = Math.max(2400, span * 8);
    this.camera.updateProjectionMatrix();
  }

  _initGroups() {
    this.markerRoot = new THREE.Group();
    this.markerRoot.name = 'markers';
    this.scene.add(this.markerRoot);

    this.linkRoot = new THREE.Group();
    this.linkRoot.name = 'links';
    this.scene.add(this.linkRoot);

    // W10：路径追踪（发光管 + hop 编号 + 断点标记）
    this._pathRoot = new THREE.Group();
    this._pathRoot.name = 'path-trace';
    this.scene.add(this._pathRoot);

    // 所有告警光环共用一个 InstancedMesh（避免每台告警设备一个网格）
    const haloGeometry = new THREE.RingGeometry(3.2, 4.5, 40);
    const haloMaterial = new THREE.MeshBasicMaterial({
      color: 0xff3b52,
      transparent: true,
      opacity: 0.55,
      side: THREE.DoubleSide,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    this.haloMesh = new THREE.InstancedMesh(haloGeometry, haloMaterial, MAX_HALOS);
    this.haloMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.haloMesh.count = 0;
    this.haloMesh.frustumCulled = false;
    this.scene.add(this.haloMesh);

    // 选中环：全局复用一个对象
    this.selectionRing = new THREE.Mesh(
      new THREE.RingGeometry(4.1, 5.2, 48),
      new THREE.MeshBasicMaterial({
        color: 0x5fe0ff,
        transparent: true,
        opacity: 0.9,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    );
    this.selectionRing.rotation.x = -Math.PI / 2;
    this.selectionRing.visible = false;
    this.scene.add(this.selectionRing);

    this._haloDummy = new THREE.Object3D();
  }

  _initInteractions() {
    this._raycaster = new THREE.Raycaster();
    this._pointer = new THREE.Vector2();
    this._hoverAt = 0;
    this._down = null;
    this.domElement.style.cursor = 'grab';

    this._onPointerDown = (event) => {
      if (event.button !== 0) return;
      this._down = { x: event.clientX, y: event.clientY, time: performance.now() };
    };

    this._onPointerUp = (event) => {
      if (event.button !== 0 || !this._down) return;
      const delta = Math.hypot(event.clientX - this._down.x, event.clientY - this._down.y);
      const elapsed = performance.now() - this._down.time;
      this._down = null;
      // 拖拽旋转 / 长按不视为点击，避免误选中
      if (delta > 6 || elapsed > 600) return;
      const item = this._pick(event.clientX, event.clientY);
      if (item) this.selectItem(item.key);
      else this.clearSelection();
    };

    this._onPointerMove = (event) => {
      if (this._down) return;
      const now = performance.now();
      if (now - this._hoverAt < 50) return; // 节流，避免高频射线检测
      this._hoverAt = now;
      const item = this._pick(event.clientX, event.clientY);
      this.domElement.style.cursor = item ? 'pointer' : 'grab';
      this.onHover(item, event);
    };

    this._onPointerLeave = () => this.onHover(null, null);

    this._onKeyDown = (event) => {
      if (event.key === 'Escape') this.clearSelection();
    };

    this.domElement.addEventListener('pointerdown', this._onPointerDown);
    this.domElement.addEventListener('pointerup', this._onPointerUp);
    this.domElement.addEventListener('pointermove', this._onPointerMove);
    this.domElement.addEventListener('pointerleave', this._onPointerLeave);
    document.addEventListener('keydown', this._onKeyDown);
  }

  _initResize() {
    this._resize = () => {
      const width = this.container.clientWidth || window.innerWidth;
      const height = this.container.clientHeight || window.innerHeight;
      if (!width || !height) return;
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(width, height);
    };
    this._resizeObserver = new ResizeObserver(this._resize);
    this._resizeObserver.observe(this.container);
    window.addEventListener('resize', this._resize);
  }

  _startIntro() {
    this.camera.position.copy(CAMERA_START);
    this.controls.target.copy(this._defaultTarget);
    // 开场镜头从高处推近，增强大屏演示效果
    this._tweenCamera(this._defaultCamera, this._defaultTarget, 1800);
  }

  // ------------------------------------------------------------------ //
  // 数据 → 场景
  // ------------------------------------------------------------------ //
  /**
   * 应用最新场景条目与链路。
   * 设备集合不变时只原地改颜色/告警状态，不重建对象（保证性能与选中态稳定）。
   */
  applyItems(items = [], links = []) {
    const nextKeys = new Set(items.map((item) => item.key));

    // 1. 移除已消失的设备
    for (const [key, marker] of this._markers) {
      if (nextKeys.has(key)) continue;
      this.markerRoot.remove(marker.group);
      disposeMarker(marker);
      this._markers.delete(key);
    }

    // 2. 新增 / 更新设备标记（首次加载不闪烁；后续新增设备自动高亮 3 秒）
    const highlightNew = this._hasAppliedItems;
    let newMarkerCount = 0;
    for (const item of items) {
      let marker = this._markers.get(item.key);
      if (!marker) {
        marker = createMarker(item);
        this.markerRoot.add(marker.group);
        this._markers.set(item.key, marker);
        if (highlightNew) {
          marker.highlightUntil = performance.now() + HIGHLIGHT_MS;
          newMarkerCount += 1;
        }
      } else {
        updateMarker(marker, item);
        marker.group.position.set(item.position.x, item.position.y, item.position.z);
      }
      marker.item = item;
    }
    this._lastNewMarkerCount = newMarkerCount;
    this._hasAppliedItems = true;

    this._items = items;
    this._itemsByKey = new Map(items.map((item) => [item.key, item]));

    // 3. 重建可拾取网格列表
    this._pickMeshes = [];
    for (const marker of this._markers.values()) this._pickMeshes.push(...marker.meshes);

    // 4. 告警设备（脉冲高亮 + 光环）
    this._alertMarkers = items
      .filter((item) => item.alert)
      .map((item) => this._markers.get(item.key))
      .filter(Boolean);

    // 5. 拓扑链路
    this._rebuildLinks(links);

    // 6. 选中状态在数据刷新后保持一致
    if (this._selectedKey && !this._itemsByKey.has(this._selectedKey)) {
      this.clearSelection();
    } else if (this._selectedKey) {
      this._updateSelectionRing();
    }
  }

  _rebuildLinks(links = []) {
    while (this.linkRoot.children.length) {
      const child = this.linkRoot.children.pop();
      child.geometry?.dispose();
      child.material?.dispose();
    }
    const positionByKey = new Map(this._items.map((item) => [item.key, item.position]));
    for (const link of links) {
      const from = positionByKey.get(`device:${link.source}`);
      const to = positionByKey.get(`device:${link.target}`);
      if (!from || !to) continue; // 链路端点不在当前设备快照里时跳过（不虚构节点）
      const geometry = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(from.x, from.y, from.z),
        new THREE.Vector3(to.x, to.y, to.z),
      ]);
      const material = new THREE.LineBasicMaterial({
        color: link.up === false ? 0xff5266 : 0x2f86c4,
        transparent: true,
        opacity: link.up === false ? 0.9 : 0.5,
      });
      this.linkRoot.add(new THREE.Line(geometry, material));
    }
  }

  // ------------------------------------------------------------------ //
  // 选中 / 相机
  // ------------------------------------------------------------------ //
  selectItem(key, options = {}) {
    const item = this._itemsByKey.get(key);
    if (!item) return false;
    this._selectedKey = key;
    this._updateSelectionRing();
    if (options.focus !== false) this._focusItem(item);
    this.onSelect(item);
    return true;
  }

  clearSelection() {
    const hadSelection = Boolean(this._selectedKey);
    this._selectedKey = null;
    this.selectionRing.visible = false;
    this.controls.autoRotate = false;
    if (hadSelection) this.onDeselect();
  }

  resetView() {
    this.clearSelection();
    this._tweenCamera(this._defaultCamera, this._defaultTarget, 900);
  }

  setAutoRotate(enabled) {
    this.autoRotate = Boolean(enabled);
  }

  getObjectStats() {
    // objects = 实际参与渲染的对象（Mesh / Line / Sprite / InstancedMesh），
    // nodes = 场景树全部节点（含 Group 容器，用于排查层级）。
    // 渲染对象已控制在 60 以内：无 LOD、同类设备共享几何体、告警光环用 InstancedMesh。
    let nodes = 0;
    let objects = 0;
    this.scene.traverse((object) => {
      nodes += 1;
      if (
        object.isMesh ||
        object.isLine ||
        object.isLineSegments ||
        object.isSprite ||
        object.isPoints ||
        object.isInstancedMesh
      ) {
        objects += 1;
      }
    });
    return {
      objects,
      nodes,
      markers: this._markers.size,
      links: this.linkRoot.children.length,
      alertMarkers: this._alertMarkers.length,
      buildings: this._buildings.length,
      pendingMarkers: this._items.filter((item) => item.pending).length,
      highlightedMarkers: this._markers.size
        ? [...this._markers.values()].filter((marker) => marker.highlightUntil > performance.now()).length
        : 0,
      lastNewMarkers: this._lastNewMarkerCount || 0,
      buildingHighlighting: Boolean(
        this._buildingHighlightUntil && performance.now() < this._buildingHighlightUntil,
      ),
      selected: this._selectedKey,
      autoRotate: this.autoRotate,
      pathActive: Boolean(this._pathTrace && this._pathHops.length >= 2),
      pathHops: this._pathHops.length,
      pathBreakpoint: Boolean(this._pathBreakpoint),
    };
  }

  // ------------------------------------------------------------------ //
  // W10：路径追踪 3D 可视化
  // 发光管沿 hop 路径高亮 + hop 编号/名称 + 方向脉冲；partial/failed 显示红色 ✕ 断点。
  // ------------------------------------------------------------------ //
  _disposePathObjects() {
    if (!this._pathRoot) return;
    for (const child of this._pathRoot.children.slice()) {
      child.traverse?.((node) => {
        if (node.userData?.isLabel) disposeLabelSprite(node);
      });
      child.geometry?.dispose?.();
      if (child.material) {
        const materials = Array.isArray(child.material) ? child.material : [child.material];
        for (const material of materials) {
          material.map?.dispose?.();
          material.dispose?.();
        }
      }
      this._pathRoot.remove(child);
    }
    this._pathPulses = [];
    this._pathHops = [];
    this._pathBreakpoint = null;
    this._pathCurve = null;
    this._pathStats = { active: false, hops: 0, breakpoint: false };
  }

  /** 清理路径高亮（不改变相机） */
  clearPath() {
    this._disposePathObjects();
    this._pathTrace = null;
  }

  /** hop 设备名/设备 id → 3D 坐标；匹配不到返回 null（不虚构节点） */
  _resolveHopPosition(hop = {}) {
    const name = String(hop.device_name || '').trim().toLowerCase();
    const deviceId = String(hop.device_id || '').trim();
    for (const marker of this._markers.values()) {
      const item = marker.item;
      if (!item) continue;
      if (name && String(item.hostname || '').toLowerCase() === name) {
        return marker.group.position.clone();
      }
      if (deviceId && String(item.id || '') === deviceId) {
        return marker.group.position.clone();
      }
    }
    return null;
  }

  /**
   * 渲染一次路径追踪结果。
   * @param {object} trace POST /api/path-trace 响应
   * @param {object} options focus=true 时把镜头拉到路径包围盒
   */
  setPathTrace(trace, { focus = true } = {}) {
    this._disposePathObjects();
    this._pathTrace = trace && typeof trace === 'object' ? trace : null;
    const rawHops = Array.isArray(trace?.hops) ? trace.hops : [];
    const hops = [];
    for (const hop of rawHops) {
      const position = this._resolveHopPosition(hop);
      if (position) hops.push({ ...hop, position });
    }
    if (hops.length < 2) {
      this._pathStats = { active: false, hops: hops.length, breakpoint: false };
      return false;
    }

    const roleColor = (hop, index) => {
      const role = String(hop.role || '').toLowerCase();
      if (role === 'source') return 0x38e08a;
      if (role === 'destination') return 0xffc857;
      return index % 2 === 0 ? 0x4cc9f0 : 0x7d7bff;
    };
    const roleColorCss = (hop) => {
      const role = String(hop.role || '').toLowerCase();
      if (role === 'source') return '#38e08a';
      if (role === 'destination') return '#ffc857';
      return '#4cc9f0';
    };

    // 路径点抬高到楼体上方，段间加抬升中点形成平滑弧线
    const points = hops.map((hop) => {
      const point = hop.position.clone();
      point.y = Math.max(hop.position.y + 8, 20);
      return point;
    });
    const curvePoints = [];
    for (let index = 0; index < points.length; index += 1) {
      if (index > 0) {
        const mid = points[index - 1].clone().lerp(points[index], 0.5);
        mid.y += 7;
        curvePoints.push(mid);
      }
      curvePoints.push(points[index]);
    }
    const curve = new THREE.CatmullRomCurve3(curvePoints, false, 'catmullrom', 0.35);
    this._pathCurve = curve;

    // 外层光晕管 + 内层亮管 + 中心亮线（加法混合，形成“发光路径”）
    const glowTube = new THREE.Mesh(
      new THREE.TubeGeometry(curve, 96, 2.1, 10, false),
      new THREE.MeshBasicMaterial({
        color: 0x1f6fbf,
        transparent: true,
        opacity: 0.28,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    );
    glowTube.renderOrder = 5;
    this._pathRoot.add(glowTube);

    const tube = new THREE.Mesh(
      new THREE.TubeGeometry(curve, 96, 0.72, 10, false),
      new THREE.MeshBasicMaterial({
        color: 0x4cc9f0,
        transparent: true,
        opacity: 0.85,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    );
    tube.renderOrder = 6;
    this._pathRoot.add(tube);

    const coreLine = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(curve.getPoints(140)),
      new THREE.LineBasicMaterial({
        color: 0xe8fbff,
        transparent: true,
        opacity: 0.95,
        depthWrite: false,
      }),
    );
    coreLine.renderOrder = 7;
    this._pathRoot.add(coreLine);

    // hop 节点：地面→路径光柱 + 圆环 + “编号 · 设备名”标签
    hops.forEach((hop, index) => {
      const point = points[index];
      const color = roleColor(hop, index);
      const beamHeight = Math.max(2, point.y - hop.position.y);
      const beam = new THREE.Mesh(
        new THREE.CylinderGeometry(0.22, 0.22, beamHeight, 6),
        new THREE.MeshBasicMaterial({
          color,
          transparent: true,
          opacity: 0.35,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
        }),
      );
      beam.position.set(hop.position.x, hop.position.y + beamHeight / 2, hop.position.z);
      this._pathRoot.add(beam);

      const ring = new THREE.Mesh(
        new THREE.RingGeometry(2.6, 3.5, 36),
        new THREE.MeshBasicMaterial({
          color,
          transparent: true,
          opacity: 0.9,
          side: THREE.DoubleSide,
          depthWrite: false,
          blending: THREE.AdditiveBlending,
        }),
      );
      ring.rotation.x = -Math.PI / 2;
      ring.position.copy(point);
      ring.userData.isPathRing = true;
      ring.userData.pathIndex = index;
      this._pathRoot.add(ring);

      const label = makeLabelSprite(`${index + 1} ${hop.device_name || `hop${index}`}`, {
        worldHeight: 3.4,
        fontSize: 34,
        accent: roleColorCss(hop),
        color: '#f2fbff',
        background: 'rgba(4, 14, 24, 0.82)',
      });
      // 相邻 hop 标签上下错开，避免同一楼栋内多个编号互相重叠
      label.position.set(point.x, point.y + 7.2 + (index % 2) * 4.6, point.z);
      label.userData.isPathHop = true;
      label.userData.hopIndex = index;
      this._pathRoot.add(label);
    });

    // 断点：partial/failed 时最后一个可达 hop 显示红色 ✕ + 红色脉冲环 + 红色光束
    const traceStatus = String(trace.trace_status || trace.status || 'complete').toLowerCase();
    if (traceStatus !== 'complete') {
      const lastHop = hops[hops.length - 1];
      const lastPoint = points[points.length - 1];
      this._pathBreakpoint = {
        device: lastHop.device_name || '未知设备',
        reason: trace.error || '路径不完整（部分 hop 不可达）',
      };
      const breakRing = new THREE.Mesh(
        new THREE.RingGeometry(4.4, 5.8, 40),
        new THREE.MeshBasicMaterial({
          color: 0xff3b52,
          transparent: true,
          opacity: 0.95,
          side: THREE.DoubleSide,
          depthWrite: false,
          blending: THREE.AdditiveBlending,
        }),
      );
      breakRing.rotation.x = -Math.PI / 2;
      breakRing.position.copy(lastPoint);
      breakRing.userData.isPathBreakRing = true;
      this._pathRoot.add(breakRing);

      const breakBeam = new THREE.Mesh(
        new THREE.CylinderGeometry(0.5, 0.5, 46, 8),
        new THREE.MeshBasicMaterial({
          color: 0xff3b52,
          transparent: true,
          opacity: 0.22,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
        }),
      );
      breakBeam.position.set(lastPoint.x, lastPoint.y + 14, lastPoint.z);
      this._pathRoot.add(breakBeam);

      const breakLabel = makeLabelSprite(`✕ 断点 · ${this._pathBreakpoint.device}`, {
        worldHeight: 4.1,
        fontSize: 36,
        accent: '#ff5266',
        color: '#ffe1e5',
        background: 'rgba(52, 8, 14, 0.92)',
      });
      breakLabel.position.set(lastPoint.x, lastPoint.y + 15, lastPoint.z);
      breakLabel.userData.isPathBreak = true;
      this._pathRoot.add(breakLabel);

      // 最后一段路径改为红色，突出断点前的位置
      if (points.length >= 2) {
        const breakCurve = new THREE.CatmullRomCurve3([
          points[points.length - 2].clone(),
          lastPoint.clone(),
        ]);
        const breakTube = new THREE.Mesh(
          new THREE.TubeGeometry(breakCurve, 32, 0.9, 8, false),
          new THREE.MeshBasicMaterial({
            color: 0xff3b52,
            transparent: true,
            opacity: 0.75,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
          }),
        );
        this._pathRoot.add(breakTube);
      }
    }

    // 方向脉冲：2 个发光小球沿路径循环移动，直观显示“流向”
    for (let index = 0; index < 2; index += 1) {
      const pulse = new THREE.Mesh(
        new THREE.SphereGeometry(1.05, 12, 12),
        new THREE.MeshBasicMaterial({
          color: 0xd8f6ff,
          transparent: true,
          opacity: 0.9,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
        }),
      );
      pulse.userData.offset = index * 0.5;
      this._pathRoot.add(pulse);
      this._pathPulses.push(pulse);
    }

    this._pathHops = hops;
    this._pathStats = {
      active: true,
      hops: hops.length,
      breakpoint: Boolean(this._pathBreakpoint),
      status: traceStatus,
    };

    // 镜头拉到路径包围盒，保证路径与 hop 编号都在画面内
    if (focus) {
      const box = new THREE.Box3().setFromPoints(points);
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());
      const span = Math.max(size.x, size.z, 120);
      this._tweenCamera(
        new THREE.Vector3(center.x, center.y + 88 + span * 0.2, center.z + 130 + span * 0.45),
        new THREE.Vector3(center.x, center.y - 6, center.z),
        1200,
      );
    }
    return true;
  }

  getPathStats() {
    return {
      ...this._pathStats,
      objects: this._pathRoot ? this._pathRoot.children.length : 0,
      labels: this._pathRoot
        ? this._pathRoot.children.filter((child) => child.userData?.isPathHop).length
        : 0,
      pulses: this._pathPulses.length,
      breakpointDevice: this._pathBreakpoint?.device || null,
    };
  }

  _focusItem(item) {
    const { x, y, z } = item.position;
    const target = new THREE.Vector3(x, y + 0.6, z);
    const direction = new THREE.Vector3(
      x - this._campusCenter.x,
      0,
      z - this._campusCenter.z,
    );
    if (direction.lengthSq() < 1) direction.set(0, 0, 1);
    direction.normalize();
    const toPosition = new THREE.Vector3(
      target.x + direction.x * 82,
      target.y + 36,
      target.z + direction.z * 82,
    );
    this._tweenCamera(toPosition, target, 850);
  }

  _updateSelectionRing() {
    const item = this._itemsByKey.get(this._selectedKey);
    if (!item) {
      this.selectionRing.visible = false;
      return;
    }
    this.selectionRing.position.set(item.position.x, item.position.y - 1.6, item.position.z);
    this.selectionRing.visible = true;
  }

  _tweenCamera(toPosition, toTarget, duration) {
    this._cameraTween = {
      fromPosition: this.camera.position.clone(),
      toPosition: toPosition.clone(),
      fromTarget: this.controls.target.clone(),
      toTarget: toTarget.clone(),
      start: performance.now(),
      duration: Math.max(duration, 1),
    };
  }

  _updateCameraTween() {
    const tween = this._cameraTween;
    if (!tween) return;
    const progress = Math.min(1, (performance.now() - tween.start) / tween.duration);
    const eased = easeInOutCubic(progress);
    this.camera.position.lerpVectors(tween.fromPosition, tween.toPosition, eased);
    this.controls.target.lerpVectors(tween.fromTarget, tween.toTarget, eased);
    if (progress >= 1) this._cameraTween = null;
  }

  // ------------------------------------------------------------------ //
  // 拾取
  // ------------------------------------------------------------------ //
  _pick(clientX, clientY) {
    const rect = this.domElement.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    this._pointer.set(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    );
    this._raycaster.setFromCamera(this._pointer, this.camera);
    const intersections = this._raycaster.intersectObjects(this._pickMeshes, false);
    for (const hit of intersections) {
      const item = this._itemsByKey.get(hit.object.userData.key);
      if (item) return item;
    }
    return null;
  }

  // ------------------------------------------------------------------ //
  // 每帧动画
  // ------------------------------------------------------------------ //
  _animate() {
    const elapsed = this._clock.getElapsedTime();
    this._updateCameraTween();

    // 选中设备或相机动画期间暂停自动旋转
    this.controls.autoRotate = this.autoRotate && !this._selectedKey && !this._cameraTween;
    this.controls.update();

    // 告警设备：材质自发光呼吸（脉冲高亮）
    for (const marker of this._alertMarkers) {
      const wave = 0.5 + 0.5 * Math.sin(elapsed * 3.2 + marker.phase);
      marker.material.emissiveIntensity = 0.35 + 0.65 * wave;
    }

    // W9：新设备出现后高亮 3 秒（放大 + 发光呼吸），到期恢复
    const now = performance.now();
    for (const marker of this._markers.values()) {
      if (marker.highlightUntil && now < marker.highlightUntil) {
        const wave = 0.5 + 0.5 * Math.sin(elapsed * 7 + marker.phase);
        marker.group.scale.setScalar(1 + 0.18 * wave);
        marker.material.emissiveIntensity = Math.max(
          marker.material.emissiveIntensity,
          0.45 + 0.55 * wave,
        );
      } else if (marker.highlightUntil) {
        marker.highlightUntil = 0;
        marker.group.scale.setScalar(1);
        marker.material.emissiveIntensity = marker.pending
          ? 0.18
          : marker.item?.alert
            ? 0.6
            : 0.32;
      }
    }

    // W9：新楼栋出现后楼体自发光高亮 3 秒
    const buildingHighlighting =
      this._buildingHighlightUntil && now < this._buildingHighlightUntil;
    for (let index = 0; index < this._buildingBodies.length; index += 1) {
      const body = this._buildingBodies[index];
      body.material.emissiveIntensity = buildingHighlighting
        ? 0.08 + 0.22 * (0.5 + 0.5 * Math.sin(elapsed * 6 + index))
        : 0.08;
    }
    if (this._buildingHighlightUntil && !buildingHighlighting) this._buildingHighlightUntil = 0;

    this._updateHalos(elapsed);
    this._updateBuildingLabels();
    // 聚焦某个设备时只保留它的名字标签；路径追踪激活时隐藏设备标签，突出 hop 编号
    for (const marker of this._markers.values()) {
      marker.label.visible =
        !this._pathTrace && (!this._selectedKey || marker.key === this._selectedKey);
    }

    // 选中环：旋转 + 呼吸缩放
    if (this.selectionRing.visible) {
      this.selectionRing.rotation.z += 0.012;
      this.selectionRing.scale.setScalar(1 + 0.07 * Math.sin(elapsed * 3));
    }

    // NC 控制器：旋转八面体与能量环
    const controllerMarker = this._markers.get('controller:nc-controller');
    if (controllerMarker) {
      const mesh = controllerMarker.meshes[0];
      if (mesh) mesh.rotation.y += 0.012;
      const ring = controllerMarker.group.children.find((child) => child.userData?.isControllerRing);
      if (ring) ring.rotation.z += 0.008;
    }

    // W10：路径追踪动画 — 方向脉冲沿路径流动 + hop 圆环/断点环呼吸
    if (this._pathCurve && this._pathHops.length >= 2) {
      for (const pulse of this._pathPulses) {
        const progress = (elapsed * 0.16 + (pulse.userData.offset || 0)) % 1;
        pulse.position.copy(this._pathCurve.getPointAt(progress));
        pulse.scale.setScalar(0.85 + 0.45 * Math.sin(elapsed * 6 + progress * 12));
      }
      for (const child of this._pathRoot.children) {
        if (child.userData?.isPathRing) {
          const wave = 0.5 + 0.5 * Math.sin(elapsed * 3 + child.userData.pathIndex * 0.8);
          child.scale.setScalar(1 + 0.18 * wave);
          child.material.opacity = 0.6 + 0.35 * wave;
        } else if (child.userData?.isPathBreakRing) {
          const wave = 0.5 + 0.5 * Math.sin(elapsed * 4.2);
          child.scale.setScalar(1 + 0.3 * wave);
          child.material.opacity = 0.5 + 0.45 * wave;
        }
      }
    }

    this.renderer.render(this.scene, this.camera);
  }

  /** 镜头拉近或聚焦设备时隐藏/淡出楼栋大标签，避免文字贴脸遮挡画面 */
  _updateBuildingLabels() {
    if (!this.buildingLabels) return;
    for (const label of this.buildingLabels) {
      const distance = this.camera.position.distanceTo(label.position);
      // 路径追踪激活时隐藏楼栋名，给 hop 编号/断点标签让位
      label.visible = !this._selectedKey && !this._pathTrace && distance > 130;
      if (label.visible) {
        label.material.opacity = Math.min(1, Math.max(0.3, (distance - 130) / 90));
      }
    }
  }

  _updateHalos(elapsed) {
    const count = Math.min(this._alertMarkers.length, MAX_HALOS);
    this.haloMesh.count = count;
    if (count === 0) return;
    for (let index = 0; index < count; index += 1) {
      const marker = this._alertMarkers[index];
      const wave = 0.5 + 0.5 * Math.sin(elapsed * 2.6 + marker.phase);
      const position = marker.group.position;
      this._haloDummy.position.set(position.x, position.y - 1.5, position.z);
      this._haloDummy.rotation.set(-Math.PI / 2, 0, 0);
      this._haloDummy.scale.setScalar(1 + 0.35 * wave);
      this._haloDummy.updateMatrix();
      this.haloMesh.setMatrixAt(index, this._haloDummy.matrix);
    }
    this.haloMesh.instanceMatrix.needsUpdate = true;
    this.haloMesh.material.opacity = 0.3 + 0.3 * (0.5 + 0.5 * Math.sin(elapsed * 2.6));
  }

  dispose() {
    this._disposePathObjects();
    this.renderer.setAnimationLoop(null);
    this._resizeObserver?.disconnect();
    window.removeEventListener('resize', this._resize);
    document.removeEventListener('keydown', this._onKeyDown);
    this.domElement.removeEventListener('pointerdown', this._onPointerDown);
    this.domElement.removeEventListener('pointerup', this._onPointerUp);
    this.domElement.removeEventListener('pointermove', this._onPointerMove);
    this.domElement.removeEventListener('pointerleave', this._onPointerLeave);
    this.renderer.dispose();
  }
}
