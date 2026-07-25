import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

import type { CloudFrame, PoseMessage } from "./protocol";

// Streamer caps map snapshots at 150k and scans at 60k; headroom avoids
// reallocation.
const MAX_POINTS = 200_000;
const MAX_SCAN_POINTS = 80_000;
const BG_COLOR = 0x04060c;
const TRAIL_CAPACITY = 512;
const TRAIL_MIN_STEP_M = 0.05;
// First-person eye height above the robot base, meters.
const POV_EYE_HEIGHT = 0.45;
const POV_LOOK_HEIGHT = 0.35;

export type ViewMode = "orbit" | "top" | "pov";

export interface WorldPosition {
  x: number;
  y: number;
  z: number;
}

export interface LidarScene {
  updateCloud(frame: CloudFrame): void;
  updatePremap(frame: CloudFrame): void;
  updateScan(frame: CloudFrame): void;
  updatePose(pose: PoseMessage): void;
  setBedroom(position: WorldPosition | null): void;
  setBedroomDraft(position: WorldPosition | null): void;
  pickGround(clientX: number, clientY: number): WorldPosition | null;
  getRobotPose(): WorldPosition | null;
  setViewMode(mode: ViewMode): void;
  resetView(): void;
  focusRobot(): boolean;
  getFps(): number;
  dispose(): void;
}

const POINT_VERTEX_SHADER = /* glsl */ `
  uniform float uMinH;
  uniform float uMaxH;
  uniform float uSize;
  uniform float uPr;
  varying float vH;

  void main() {
    // Height ramp reads dimos-frame z (local z inside the rotated group).
    vH = clamp((position.z - uMinH) / max(uMaxH - uMinH, 1e-4), 0.0, 1.0);
    vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = clamp(uSize / -mvPosition.z, 1.2 * uPr, 7.0 * uPr);
    gl_Position = projectionMatrix * mvPosition;
  }
`;

const POINT_FRAGMENT_SHADER = /* glsl */ `
  uniform float uTime;
  uniform float uAlpha;
  uniform vec3 uColorLow;
  uniform vec3 uColorMid;
  uniform vec3 uColorHigh;
  varying float vH;

  void main() {
    float d = length(gl_PointCoord - vec2(0.5));
    float alpha = smoothstep(0.5, 0.15, d);
    if (alpha < 0.02) discard;

    vec3 color = mix(uColorLow, uColorMid, smoothstep(0.0, 0.55, vH));
    color = mix(color, uColorHigh, smoothstep(0.55, 1.0, vH));

    float shimmer = 0.9 + 0.1 * sin(uTime * 2.0 + vH * 20.0);
    gl_FragColor = vec4(color * shimmer, alpha * uAlpha);
  }
`;

interface PointLayerColors {
  low: number;
  mid: number;
  high: number;
}

function makePointsMaterial(
  colors: PointLayerColors,
  size: number,
  alpha: number,
  pixelRatio: number
): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    vertexShader: POINT_VERTEX_SHADER,
    fragmentShader: POINT_FRAGMENT_SHADER,
    uniforms: {
      uMinH: { value: 0 },
      uMaxH: { value: 1 },
      uTime: { value: 0 },
      uSize: { value: size * pixelRatio },
      uPr: { value: pixelRatio },
      uAlpha: { value: alpha },
      uColorLow: { value: new THREE.Color(colors.low) },
      uColorMid: { value: new THREE.Color(colors.mid) },
      uColorHigh: { value: new THREE.Color(colors.high) },
    },
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
}

function makePointLayer(
  capacity: number,
  material: THREE.ShaderMaterial
): { points: THREE.Points; geometry: THREE.BufferGeometry; attr: THREE.BufferAttribute } {
  const geometry = new THREE.BufferGeometry();
  const attr = new THREE.BufferAttribute(new Float32Array(capacity * 3), 3);
  attr.setUsage(THREE.DynamicDrawUsage);
  geometry.setAttribute("position", attr);
  geometry.setDrawRange(0, 0);
  const points = new THREE.Points(geometry, material);
  points.frustumCulled = false;
  return { points, geometry, attr };
}

function makeGlowTexture(): THREE.Texture {
  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const gradient = ctx.createRadialGradient(
    size / 2,
    size / 2,
    0,
    size / 2,
    size / 2,
    size / 2
  );
  gradient.addColorStop(0, "rgba(120, 240, 255, 0.9)");
  gradient.addColorStop(0.35, "rgba(60, 190, 240, 0.35)");
  gradient.addColorStop(1, "rgba(0, 60, 120, 0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function makeLabelTexture(label: string): THREE.CanvasTexture {
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 128;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "rgba(4, 6, 12, 0.88)";
  ctx.fillRect(8, 8, 496, 112);
  ctx.strokeStyle = "#f472b6";
  ctx.lineWidth = 8;
  ctx.strokeRect(8, 8, 496, 112);
  ctx.fillStyle = "#fce7f3";
  ctx.font = "700 54px monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, 256, 67);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function heightPercentiles(positions: Float32Array, count: number): [number, number] {
  const samples = Math.min(2048, count);
  if (samples === 0) return [0, 1];
  const stride = Math.max(1, Math.floor(count / samples));
  const zs: number[] = [];
  for (let i = 0; i < count; i += stride) {
    zs.push(positions[i * 3 + 2]);
  }
  zs.sort((a, b) => a - b);
  const lo = zs[Math.floor(zs.length * 0.02)];
  const hi = zs[Math.min(zs.length - 1, Math.floor(zs.length * 0.98))];
  return hi - lo < 1e-3 ? [lo - 0.5, lo + 0.5] : [lo, hi];
}

export function createLidarScene(canvas: HTMLCanvasElement): LidarScene {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  const pixelRatio = Math.min(window.devicePixelRatio, 2);
  renderer.setPixelRatio(pixelRatio);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(BG_COLOR);
  scene.fog = new THREE.FogExp2(BG_COLOR, 0.018);

  const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 500);
  camera.position.set(7, 5, 7);

  // Dimos world frame is Z-up; three.js is Y-up. One rotation on a parent
  // group maps (x, y, z) to (x, z, -y) so all point and pose data stays raw.
  const worldGroup = new THREE.Group();
  worldGroup.rotation.x = -Math.PI / 2;
  scene.add(worldGroup);

  // --- accumulated map layer (cool ramp) ---
  const pointsMaterial = makePointsMaterial(
    { low: 0x0b2a6f, mid: 0x22d3ee, high: 0xeafffb },
    26,
    0.85,
    pixelRatio
  );
  const mapLayer = makePointLayer(MAX_POINTS, pointsMaterial);
  worldGroup.add(mapLayer.points);

  // --- persisted premap (amber reference until relocalization aligns it) ---
  const premapMaterial = makePointsMaterial(
    { low: 0x713f12, mid: 0xf59e0b, high: 0xfff3c4 },
    22,
    0.48,
    pixelRatio
  );
  const premapLayer = makePointLayer(MAX_POINTS, premapMaterial);
  premapLayer.points.renderOrder = -1;
  worldGroup.add(premapLayer.points);

  // --- live scan layer (hot, brighter, drawn over the map) ---
  const scanMaterial = makePointsMaterial(
    { low: 0x0891b2, mid: 0x67e8f9, high: 0xffffff },
    34,
    1.0,
    pixelRatio
  );
  const scanLayer = makePointLayer(MAX_SCAN_POINTS, scanMaterial);
  scanLayer.points.renderOrder = 1;
  worldGroup.add(scanLayer.points);

  // --- floor grid (three world space; y = 0 equals dimos ground z = 0) ---
  const grid = new THREE.PolarGridHelper(12, 16, 8, 64, 0x0e7490, 0x164e63);
  const gridMaterial = grid.material as THREE.Material;
  gridMaterial.transparent = true;
  gridMaterial.opacity = 0.15;
  gridMaterial.depthWrite = false;
  scene.add(grid);

  // --- robot marker ---
  const robotGroup = new THREE.Group();
  worldGroup.add(robotGroup);

  const coneGeometry = new THREE.ConeGeometry(0.12, 0.4, 16);
  coneGeometry.rotateZ(-Math.PI / 2); // cone tip points along +x (dimos heading)
  const coneMaterial = new THREE.MeshBasicMaterial({ color: 0x67e8f9 });
  const cone = new THREE.Mesh(coneGeometry, coneMaterial);
  cone.position.z = 0.15;
  robotGroup.add(cone);

  const glowTexture = makeGlowTexture();
  const glowMaterial = new THREE.SpriteMaterial({
    map: glowTexture,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    transparent: true,
  });
  const glow = new THREE.Sprite(glowMaterial);
  glow.scale.setScalar(1.4);
  glow.position.z = 0.15;
  robotGroup.add(glow);

  const ringGeometry = new THREE.RingGeometry(0.26, 0.3, 48);
  const ringMaterial = new THREE.MeshBasicMaterial({
    color: 0x22d3ee,
    transparent: true,
    opacity: 0.6,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  const ring = new THREE.Mesh(ringGeometry, ringMaterial);
  ring.position.z = 0.02;
  robotGroup.add(ring);

  // --- unique Bedroom marker ---
  const bedroomGroup = new THREE.Group();
  bedroomGroup.visible = false;
  worldGroup.add(bedroomGroup);
  const bedroomSphereGeometry = new THREE.SphereGeometry(0.18, 18, 12);
  const bedroomMaterial = new THREE.MeshBasicMaterial({ color: 0xf472b6 });
  const bedroomSphere = new THREE.Mesh(bedroomSphereGeometry, bedroomMaterial);
  bedroomSphere.position.z = 0.28;
  bedroomGroup.add(bedroomSphere);
  const bedroomRingGeometry = new THREE.RingGeometry(0.28, 0.36, 48);
  const bedroomRingMaterial = new THREE.MeshBasicMaterial({
    color: 0xf9a8d4,
    transparent: true,
    opacity: 0.8,
    side: THREE.DoubleSide,
    depthWrite: false,
  });
  const bedroomRing = new THREE.Mesh(bedroomRingGeometry, bedroomRingMaterial);
  bedroomRing.position.z = 0.035;
  bedroomGroup.add(bedroomRing);
  const bedroomLabelTexture = makeLabelTexture("BEDROOM");
  const bedroomLabelMaterial = new THREE.SpriteMaterial({
    map: bedroomLabelTexture,
    transparent: true,
    depthTest: false,
  });
  const bedroomLabel = new THREE.Sprite(bedroomLabelMaterial);
  bedroomLabel.position.z = 0.72;
  bedroomLabel.scale.set(1.7, 0.425, 1);
  bedroomGroup.add(bedroomLabel);

  // A draft marker previews a manual pick without replacing the persisted
  // Bedroom until the operator explicitly confirms.
  const bedroomDraftGroup = new THREE.Group();
  bedroomDraftGroup.visible = false;
  worldGroup.add(bedroomDraftGroup);
  const bedroomDraftGeometry = new THREE.ConeGeometry(0.16, 0.48, 16);
  bedroomDraftGeometry.rotateX(Math.PI / 2);
  const bedroomDraftMaterial = new THREE.MeshBasicMaterial({
    color: 0xfacc15,
    wireframe: true,
  });
  const bedroomDraftCone = new THREE.Mesh(
    bedroomDraftGeometry,
    bedroomDraftMaterial
  );
  bedroomDraftCone.position.z = 0.3;
  bedroomDraftGroup.add(bedroomDraftCone);
  const bedroomDraftRingGeometry = new THREE.RingGeometry(0.32, 0.4, 48);
  const bedroomDraftRingMaterial = new THREE.MeshBasicMaterial({
    color: 0xfde047,
    transparent: true,
    opacity: 0.9,
    side: THREE.DoubleSide,
    depthWrite: false,
  });
  const bedroomDraftRing = new THREE.Mesh(
    bedroomDraftRingGeometry,
    bedroomDraftRingMaterial
  );
  bedroomDraftRing.position.z = 0.03;
  bedroomDraftGroup.add(bedroomDraftRing);

  // --- trail ---
  const trailPositions = new Float32Array(TRAIL_CAPACITY * 3);
  const trailColors = new Float32Array(TRAIL_CAPACITY * 3);
  const trailGeometry = new THREE.BufferGeometry();
  trailGeometry.setAttribute(
    "position",
    new THREE.BufferAttribute(trailPositions, 3).setUsage(THREE.DynamicDrawUsage)
  );
  trailGeometry.setAttribute(
    "color",
    new THREE.BufferAttribute(trailColors, 3).setUsage(THREE.DynamicDrawUsage)
  );
  trailGeometry.setDrawRange(0, 0);
  const trailMaterial = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.8,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  const trail = new THREE.Line(trailGeometry, trailMaterial);
  trail.frustumCulled = false;
  worldGroup.add(trail);
  const trailPoints: Array<[number, number, number]> = [];

  const trailNewest = new THREE.Color(0x22d3ee);
  const trailOldest = new THREE.Color(BG_COLOR);
  const scratchColor = new THREE.Color();

  function appendTrailPoint(x: number, y: number, z: number): void {
    const last = trailPoints[trailPoints.length - 1];
    if (last) {
      const dx = x - last[0];
      const dy = y - last[1];
      if (dx * dx + dy * dy < TRAIL_MIN_STEP_M * TRAIL_MIN_STEP_M) return;
    }
    trailPoints.push([x, y, z]);
    if (trailPoints.length > TRAIL_CAPACITY) trailPoints.shift();

    const n = trailPoints.length;
    for (let i = 0; i < n; i++) {
      const [px, py, pz] = trailPoints[i];
      trailPositions[i * 3] = px;
      trailPositions[i * 3 + 1] = py;
      trailPositions[i * 3 + 2] = pz + 0.05;
      scratchColor.copy(trailOldest).lerp(trailNewest, n === 1 ? 1 : i / (n - 1));
      trailColors[i * 3] = scratchColor.r;
      trailColors[i * 3 + 1] = scratchColor.g;
      trailColors[i * 3 + 2] = scratchColor.b;
    }
    trailGeometry.attributes.position.needsUpdate = true;
    trailGeometry.attributes.color.needsUpdate = true;
    trailGeometry.setDrawRange(0, n);
  }

  // --- controls ---
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxDistance = 80;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.5;
  controls.addEventListener("start", () => {
    controls.autoRotate = false;
  });

  // --- pose smoothing state (dimos coordinates) ---
  const poseTarget = { x: 0, y: 0, z: 0, yaw: 0 };
  const poseCurrent = { x: 0, y: 0, z: 0, yaw: 0 };
  let hasPose = false;
  let hasFittedCamera = false;
  let latestCloud: { positions: Float32Array; count: number } | null = null;

  // --- view mode ---
  let viewMode: ViewMode = "orbit";
  const savedOrbit = {
    position: new THREE.Vector3(),
    target: new THREE.Vector3(),
    valid: false,
  };
  const povEye = new THREE.Vector3();
  const povLook = new THREE.Vector3();
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
  const groundHit = new THREE.Vector3();

  function fitCameraToCloud(positions: Float32Array, count: number): void {
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (let i = 0; i < count; i++) {
      const x = positions[i * 3];
      const y = positions[i * 3 + 1];
      const z = positions[i * 3 + 2];
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
      if (z < minZ) minZ = z;
      if (z > maxZ) maxZ = z;
    }
    if (!Number.isFinite(minX)) return;
    const center = new THREE.Vector3(
      (minX + maxX) / 2,
      (minY + maxY) / 2,
      (minZ + maxZ) / 2
    );
    worldGroup.localToWorld(center);
    const radius = Math.max(
      2,
      Math.hypot(maxX - minX, maxY - minY, maxZ - minZ) / 2
    );
    const distance = radius * 1.8;
    const elevation = THREE.MathUtils.degToRad(35);
    controls.target.copy(center);
    camera.position.set(
      center.x + distance * Math.cos(elevation) * Math.cos(Math.PI / 4),
      center.y + distance * Math.sin(elevation),
      center.z + distance * Math.cos(elevation) * Math.sin(Math.PI / 4)
    );
    camera.up.set(0, 1, 0);
    controls.enableRotate = true;
    controls.update();
  }

  function rememberOrbit(): void {
    if (viewMode !== "orbit") return;
    savedOrbit.position.copy(camera.position);
    savedOrbit.target.copy(controls.target);
    savedOrbit.valid = true;
  }

  function restoreOrbit(): void {
    camera.up.set(0, 1, 0);
    controls.enableRotate = true;
    if (savedOrbit.valid) {
      camera.position.copy(savedOrbit.position);
      controls.target.copy(savedOrbit.target);
    } else if (latestCloud) {
      fitCameraToCloud(latestCloud.positions, latestCloud.count);
    }
    controls.update();
  }

  // --- resize ---
  function resize(): void {
    const width = canvas.clientWidth || 1;
    const height = canvas.clientHeight || 1;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(canvas);
  resize();

  // --- render loop ---
  const clock = new THREE.Clock();
  let rafId = 0;
  let frameCount = 0;
  let fps = 0;
  let fpsWindowStart = performance.now();
  let disposed = false;

  function animate(): void {
    if (disposed) return;
    rafId = requestAnimationFrame(animate);
    const dt = Math.min(clock.getDelta(), 0.1);
    const elapsed = clock.elapsedTime;

    pointsMaterial.uniforms.uTime.value = elapsed;
    premapMaterial.uniforms.uTime.value = elapsed;
    scanMaterial.uniforms.uTime.value = elapsed;

    if (hasPose) {
      const k = 1 - Math.exp(-8 * dt);
      poseCurrent.x += (poseTarget.x - poseCurrent.x) * k;
      poseCurrent.y += (poseTarget.y - poseCurrent.y) * k;
      poseCurrent.z += (poseTarget.z - poseCurrent.z) * k;
      let dyaw = poseTarget.yaw - poseCurrent.yaw;
      dyaw = ((dyaw + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
      poseCurrent.yaw += dyaw * k;
      robotGroup.position.set(poseCurrent.x, poseCurrent.y, poseCurrent.z);
      robotGroup.rotation.z = poseCurrent.yaw;
    }

    const pulse = 1 + 0.25 * Math.sin(elapsed * 3);
    ring.scale.setScalar(pulse);
    ringMaterial.opacity = 0.35 + 0.25 * (0.5 + 0.5 * Math.sin(elapsed * 3));
    bedroomRing.scale.setScalar(1 + 0.08 * Math.sin(elapsed * 2.4));
    bedroomDraftRing.scale.setScalar(1 + 0.12 * Math.sin(elapsed * 4));

    if (viewMode === "pov" && hasPose) {
      povEye.set(poseCurrent.x, poseCurrent.y, poseCurrent.z + POV_EYE_HEIGHT);
      worldGroup.localToWorld(povEye);
      camera.position.copy(povEye);
      povLook.set(
        poseCurrent.x + Math.cos(poseCurrent.yaw) * 2,
        poseCurrent.y + Math.sin(poseCurrent.yaw) * 2,
        poseCurrent.z + POV_LOOK_HEIGHT
      );
      worldGroup.localToWorld(povLook);
      camera.lookAt(povLook);
    } else {
      controls.update();
    }
    renderer.render(scene, camera);

    frameCount += 1;
    const now = performance.now();
    if (now - fpsWindowStart >= 1000) {
      fps = (frameCount * 1000) / (now - fpsWindowStart);
      frameCount = 0;
      fpsWindowStart = now;
    }
  }
  animate();

  function updateLayer(
    layer: { attr: THREE.BufferAttribute; geometry: THREE.BufferGeometry },
    material: THREE.ShaderMaterial,
    frame: CloudFrame,
    capacity: number
  ): number {
    const count = Math.min(frame.count, capacity);
    (layer.attr.array as Float32Array).set(frame.positions.subarray(0, count * 3));
    layer.attr.needsUpdate = true;
    layer.geometry.setDrawRange(0, count);

    const [minH, maxH] = heightPercentiles(frame.positions, count);
    material.uniforms.uMinH.value = minH;
    material.uniforms.uMaxH.value = maxH;
    return count;
  }

  return {
    updateCloud(frame: CloudFrame): void {
      const count = updateLayer(mapLayer, pointsMaterial, frame, MAX_POINTS);
      latestCloud = { positions: frame.positions, count };
      if (!hasFittedCamera && count > 0) {
        hasFittedCamera = true;
        fitCameraToCloud(frame.positions, count);
      }
    },

    updatePremap(frame: CloudFrame): void {
      const count = updateLayer(
        premapLayer,
        premapMaterial,
        frame,
        MAX_POINTS
      );
      if (!latestCloud && count > 0) {
        latestCloud = { positions: frame.positions, count };
      }
      if (!hasFittedCamera && count > 0) {
        hasFittedCamera = true;
        fitCameraToCloud(frame.positions, count);
      }
    },

    updateScan(frame: CloudFrame): void {
      updateLayer(scanLayer, scanMaterial, frame, MAX_SCAN_POINTS);
    },

    setViewMode(mode: ViewMode): void {
      if (mode === viewMode) return;
      rememberOrbit();
      if (mode === "pov") {
        controls.enabled = false;
        controls.autoRotate = false;
        robotGroup.visible = false;
      } else if (mode === "top") {
        robotGroup.visible = true;
        controls.enabled = true;
        controls.autoRotate = false;
        controls.enableRotate = false;
        const target = controls.target.clone();
        const distance = Math.max(8, camera.position.distanceTo(target));
        camera.up.set(0, 0, -1);
        camera.position.set(target.x, target.y + distance, target.z + 0.001);
        camera.lookAt(target);
        controls.update();
      } else {
        robotGroup.visible = true;
        controls.enabled = true;
        restoreOrbit();
      }
      viewMode = mode;
    },

    updatePose(pose: PoseMessage): void {
      poseTarget.x = pose.x;
      poseTarget.y = pose.y;
      poseTarget.z = pose.z;
      poseTarget.yaw = pose.yaw;
      if (!hasPose) {
        hasPose = true;
        Object.assign(poseCurrent, poseTarget);
      }
      appendTrailPoint(pose.x, pose.y, pose.z);
    },

    setBedroom(position: WorldPosition | null): void {
      bedroomGroup.visible = position !== null;
      if (position) {
        bedroomGroup.position.set(position.x, position.y, position.z);
      }
    },

    setBedroomDraft(position: WorldPosition | null): void {
      bedroomDraftGroup.visible = position !== null;
      if (position) {
        bedroomDraftGroup.position.set(position.x, position.y, position.z);
      }
    },

    pickGround(clientX: number, clientY: number): WorldPosition | null {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      if (!raycaster.ray.intersectPlane(groundPlane, groundHit)) return null;
      const local = worldGroup.worldToLocal(groundHit.clone());
      return { x: local.x, y: local.y, z: 0 };
    },

    getRobotPose(): WorldPosition | null {
      return hasPose
        ? { x: poseTarget.x, y: poseTarget.y, z: poseTarget.z }
        : null;
    },

    resetView(): void {
      if (!latestCloud) return;
      if (viewMode !== "orbit") {
        viewMode = "orbit";
        robotGroup.visible = true;
        controls.enabled = true;
      }
      fitCameraToCloud(latestCloud.positions, latestCloud.count);
      savedOrbit.position.copy(camera.position);
      savedOrbit.target.copy(controls.target);
      savedOrbit.valid = true;
    },

    focusRobot(): boolean {
      if (!hasPose) return false;
      if (viewMode !== "orbit") {
        viewMode = "orbit";
        robotGroup.visible = true;
        controls.enabled = true;
      }
      camera.up.set(0, 1, 0);
      controls.enableRotate = true;
      const target = new THREE.Vector3(
        poseTarget.x,
        poseTarget.y,
        poseTarget.z
      );
      worldGroup.localToWorld(target);
      controls.target.copy(target);
      camera.position.set(target.x + 4, target.y + 3, target.z + 4);
      controls.update();
      savedOrbit.position.copy(camera.position);
      savedOrbit.target.copy(controls.target);
      savedOrbit.valid = true;
      return true;
    },

    getFps(): number {
      return fps;
    },

    dispose(): void {
      disposed = true;
      cancelAnimationFrame(rafId);
      resizeObserver.disconnect();
      controls.dispose();
      mapLayer.geometry.dispose();
      premapLayer.geometry.dispose();
      scanLayer.geometry.dispose();
      trailGeometry.dispose();
      coneGeometry.dispose();
      ringGeometry.dispose();
      bedroomSphereGeometry.dispose();
      bedroomRingGeometry.dispose();
      bedroomDraftGeometry.dispose();
      bedroomDraftRingGeometry.dispose();
      grid.geometry.dispose();
      pointsMaterial.dispose();
      premapMaterial.dispose();
      scanMaterial.dispose();
      trailMaterial.dispose();
      coneMaterial.dispose();
      ringMaterial.dispose();
      bedroomMaterial.dispose();
      bedroomRingMaterial.dispose();
      bedroomLabelMaterial.dispose();
      bedroomDraftMaterial.dispose();
      bedroomDraftRingMaterial.dispose();
      glowMaterial.dispose();
      gridMaterial.dispose();
      glowTexture.dispose();
      bedroomLabelTexture.dispose();
      renderer.dispose();
    },
  };
}
