"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";

import type { CloudFrame, PoseMessage } from "./protocol";
import { RobotCamFeed } from "./RobotCamFeed";
import {
  createLidarScene,
  type LidarScene,
  type ViewMode,
  type WorldPosition,
} from "./scene";
import { type ConnState, useLidarSocket } from "./useLidarSocket";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const OPERATOR_URL =
  process.env.NEXT_PUBLIC_OPERATOR_URL ?? "http://127.0.0.1:5555/operator";
const LANGUAGE_STORAGE_KEY = "nightwatch.operator.language";

type Language = "zh" | "en";
type BedroomNotice =
  | { kind: "unmarked" | "aligning" | "offline" | "saving" | "no-pose" | "pick-failed" }
  | { kind: "current" | "updated" | "draft"; position: WorldPosition }
  | { kind: "error"; detail: string };

const STATUS_CLASSES: Record<ConnState, string> = {
  live: "border-emerald-600 bg-emerald-50 text-emerald-800",
  "waiting-robot": "border-amber-600 bg-amber-50 text-amber-800",
  connecting: "border-sky-600 bg-sky-50 text-sky-800",
  disconnected: "border-red-600 bg-red-50 text-red-800",
};

const COPY = {
  zh: {
    pageTitle: "守夜犬 · 三维雷达地图",
    app: "守夜犬空间控制台",
    viewTitle: "三维雷达地图",
    mapSource: "地图数据源",
    mapSourceHint: "实时读取 Go2 激光雷达、累计地图与机器人位姿",
    live: "实时连接",
    waitingRobot: "等待机器狗",
    connecting: "正在连接",
    disconnected: "连接断开",
    accumulated: "累计地图",
    savedMap: "已保存地图",
    liveScan: "实时雷达扫描",
    robotPose: "机器狗位姿",
    points: "点",
    aligned: "已对齐",
    reference: "参考坐标",
    waiting: "等待中",
    lastFrame: "最近帧",
    justNow: "刚刚",
    secondsAgo: "秒前",
    noData: "无数据",
    streamStatus: "数据流状态",
    mapView: "3D 地图",
    camera: "相机",
    orbit: "自由视角",
    top: "俯视",
    robotEyes: "机器狗视角",
    fitMap: "适配地图",
    findRobot: "定位机器狗",
    operator: "返回工作台",
    calibration: "BEDROOM 标定",
    calibrationHint: "全空间始终只有一个 Bedroom。新位置保存成功后自动覆盖旧位置。",
    bridgeOnline: "标定服务在线",
    bridgeOffline: "标定服务离线",
    stored: "当前 Bedroom",
    notStored: "尚未标记",
    robot: "机器狗当前位置",
    unavailable: "尚未收到位姿",
    markAnywhere: "在地图任意位置标记",
    markRobot: "标记机器狗当前位置",
    selecting: "请点击三维地图中的地面位置",
    draftTitle: "待确认位置",
    replaceHint: "确认后将替换原来的 Bedroom；保存失败时旧位置保持不变。",
    confirm: "确认保存",
    cancel: "取消",
    step1: "点击“在地图任意位置标记”进入十字光标模式。",
    step2: "在三维地图的地面上单击，黄色标记表示待确认位置。",
    step3: "确认保存后，粉色 BEDROOM 标记会更新为唯一有效位置。",
    unmarked: "Bedroom 尚未标记",
    aligning: "Bedroom 已保存，正在等待地图坐标对齐",
    offline: "Bedroom 标定服务离线；地图仍可查看",
    saving: "正在保存 Bedroom…",
    noPose: "尚未收到机器狗当前位置",
    pickFailed: "无法定位地面，请切换到俯视视角后重试",
    current: "Bedroom 当前位于",
    updated: "Bedroom 已更新为",
    draft: "请确认待标记位置",
    error: "更新失败",
    rejected: "请求被拒绝",
    unknownError: "未知错误",
    canvasHint: "拖动旋转 · 右键平移 · 滚轮缩放",
    selectHint: "标定模式：点击任意地面位置",
    sequence: "序列",
    fps: "帧率",
    coordinates: "坐标",
    cameraTitle: "机器狗相机",
    cameraOffline: "无信号 · 正在重试",
    cameraAlt: "机器狗第一视角相机",
    language: "语言",
    openFull: "打开完整地图 / Bedroom 标定",
    mapPoints: "地图点数",
  },
  en: {
    pageTitle: "Night Watch · 3D Lidar Map",
    app: "NIGHT WATCH SPATIAL CONSOLE",
    viewTitle: "3D LIDAR MAP",
    mapSource: "MAP DATA SOURCES",
    mapSourceHint: "Live Go2 lidar, accumulated map, and robot pose",
    live: "LIVE",
    waitingRobot: "WAITING FOR ROBOT",
    connecting: "CONNECTING",
    disconnected: "DISCONNECTED",
    accumulated: "ACCUMULATED MAP",
    savedMap: "SAVED MAP",
    liveScan: "LIVE LIDAR SCAN",
    robotPose: "ROBOT POSE",
    points: "pts",
    aligned: "aligned",
    reference: "reference frame",
    waiting: "waiting",
    lastFrame: "LAST FRAME",
    justNow: "now",
    secondsAgo: "s ago",
    noData: "no data",
    streamStatus: "STREAM STATUS",
    mapView: "3D MAP",
    camera: "CAMERA",
    orbit: "ORBIT",
    top: "TOP",
    robotEyes: "ROBOT EYES",
    fitMap: "FIT MAP",
    findRobot: "FIND ROBOT",
    operator: "BACK TO CONSOLE",
    calibration: "BEDROOM CALIBRATION",
    calibrationHint: "The entire space has one Bedroom. A successful save replaces the previous position.",
    bridgeOnline: "CALIBRATION ONLINE",
    bridgeOffline: "CALIBRATION OFFLINE",
    stored: "CURRENT BEDROOM",
    notStored: "NOT MARKED",
    robot: "ROBOT POSITION",
    unavailable: "POSE UNAVAILABLE",
    markAnywhere: "MARK ANY MAP POSITION",
    markRobot: "USE ROBOT POSITION",
    selecting: "Click a ground position in the 3D map",
    draftTitle: "PENDING POSITION",
    replaceHint: "Confirming replaces the previous Bedroom. A failed save leaves the old position unchanged.",
    confirm: "CONFIRM & SAVE",
    cancel: "CANCEL",
    step1: "Choose Mark Any Map Position to enter crosshair mode.",
    step2: "Click the ground in the 3D map. The yellow marker is a preview.",
    step3: "After confirmation, the pink BEDROOM marker becomes the only active destination.",
    unmarked: "Bedroom has not been marked",
    aligning: "Bedroom is saved and waiting for map alignment",
    offline: "Bedroom calibration is offline; the map remains viewable",
    saving: "Saving Bedroom…",
    noPose: "Robot position has not arrived",
    pickFailed: "Ground pick failed; switch to Top view and try again",
    current: "Current Bedroom",
    updated: "Bedroom updated",
    draft: "Confirm the pending position",
    error: "Update failed",
    rejected: "request rejected",
    unknownError: "unknown error",
    canvasHint: "Drag to orbit · Right-drag to pan · Scroll to zoom",
    selectHint: "Calibration mode: click any ground position",
    sequence: "SEQ",
    fps: "FPS",
    coordinates: "COORDINATES",
    cameraTitle: "ROBOT CAMERA",
    cameraOffline: "NO SIGNAL · RETRYING",
    cameraAlt: "Robot first-person camera",
    language: "Language",
    openFull: "OPEN FULL MAP / BEDROOM",
    mapPoints: "MAP POINTS",
  },
} as const;

function formatPosition(position: WorldPosition | null): string {
  if (!position) return "—";
  return `X ${position.x.toFixed(2)} · Y ${position.y.toFixed(2)} · Z ${position.z.toFixed(2)}`;
}

export function LidarViewer({
  embedded = false,
  initialLanguage,
}: {
  embedded?: boolean;
  initialLanguage?: Language;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const sceneRef = useRef<LidarScene | null>(null);
  const pendingCloudRef = useRef<CloudFrame | null>(null);
  const pendingPremapRef = useRef<CloudFrame | null>(null);
  const pendingScanRef = useRef<CloudFrame | null>(null);
  const pendingPoseRef = useRef<PoseMessage | null>(null);

  const [language, setLanguage] = useState<Language>(initialLanguage ?? "zh");
  const [fps, setFps] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [viewMode, setViewModeState] = useState<ViewMode>("orbit");
  const [camVisible, setCamVisible] = useState(true);
  const [selectingBedroom, setSelectingBedroom] = useState(false);
  const [bedroom, setBedroom] = useState<WorldPosition | null>(null);
  const [bedroomDraft, setBedroomDraft] = useState<WorldPosition | null>(null);
  const [draftAtRobot, setDraftAtRobot] = useState(false);
  const [robotPose, setRobotPose] = useState<WorldPosition | null>(null);
  const [bridgeConnected, setBridgeConnected] = useState(false);
  const [savingBedroom, setSavingBedroom] = useState(false);
  const [notice, setNotice] = useState<BedroomNotice>({ kind: "unmarked" });

  const c = COPY[language];

  useEffect(() => {
    if (initialLanguage) return;
    const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
    if (stored === "en") setLanguage("en");
  }, [initialLanguage]);

  const chooseLanguage = (next: Language) => {
    setLanguage(next);
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, next);
  };

  useEffect(() => {
    const receiveLanguage = (event: MessageEvent) => {
      if (
        event.origin !== "http://127.0.0.1:5555" &&
        event.origin !== "http://localhost:5555"
      ) {
        return;
      }
      const message = event.data as {
        type?: string;
        language?: string;
      } | null;
      if (
        message?.type !== "nightwatch-language" ||
        (message.language !== "zh" && message.language !== "en")
      ) {
        return;
      }
      setLanguage(message.language);
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, message.language);
    };
    window.addEventListener("message", receiveLanguage);
    return () => window.removeEventListener("message", receiveLanguage);
  }, []);

  useEffect(() => {
    if (!embedded) return;
    const relayViewSwap = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "v" || event.repeat) return;
      let parentOrigin = "";
      try {
        parentOrigin = new URL(document.referrer).origin;
      } catch {
        return;
      }
      if (
        parentOrigin !== "http://127.0.0.1:5555" &&
        parentOrigin !== "http://localhost:5555"
      ) {
        return;
      }
      event.preventDefault();
      window.parent.postMessage(
        { type: "nightwatch-toggle-vision" },
        parentOrigin,
      );
    };
    window.addEventListener("keydown", relayViewSwap);
    return () => window.removeEventListener("keydown", relayViewSwap);
  }, [embedded]);

  useEffect(() => {
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.title = c.pageTitle;
  }, [c.pageTitle, language]);

  const hud = useLidarSocket({
    onCloud: (frame) => {
      if (sceneRef.current) sceneRef.current.updateCloud(frame);
      else pendingCloudRef.current = frame;
    },
    onPremap: (frame) => {
      if (sceneRef.current) sceneRef.current.updatePremap(frame);
      else pendingPremapRef.current = frame;
    },
    onScan: (frame) => {
      if (sceneRef.current) sceneRef.current.updateScan(frame);
      else pendingScanRef.current = frame;
    },
    onPose: (pose) => {
      const position = { x: pose.x, y: pose.y, z: pose.z };
      setRobotPose(position);
      if (sceneRef.current) sceneRef.current.updatePose(pose);
      else pendingPoseRef.current = pose;
    },
  });

  useEffect(() => {
    if (!canvasRef.current) return;
    const scene = createLidarScene(canvasRef.current);
    sceneRef.current = scene;
    if (pendingCloudRef.current) scene.updateCloud(pendingCloudRef.current);
    if (pendingPremapRef.current) scene.updatePremap(pendingPremapRef.current);
    if (pendingScanRef.current) scene.updateScan(pendingScanRef.current);
    if (pendingPoseRef.current) scene.updatePose(pendingPoseRef.current);
    return () => {
      sceneRef.current = null;
      scene.dispose();
    };
  }, []);

  useEffect(() => {
    sceneRef.current?.setBedroom(bedroom);
  }, [bedroom]);

  useEffect(() => {
    sceneRef.current?.setBedroomDraft(bedroomDraft);
  }, [bedroomDraft]);

  useEffect(() => {
    let active = true;
    const refreshBedroom = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/robot/bedroom`, {
          cache: "no-store",
        });
        if (!response.ok) throw new Error();
        const data = (await response.json()) as {
          connected?: boolean;
          bedroom?: {
            available?: boolean;
            world_x?: number;
            world_y?: number;
            world_z?: number;
          } | null;
        };
        if (!active) return;
        setBridgeConnected(Boolean(data.connected));
        const value = data.bedroom;
        if (
          value?.available &&
          value.world_x != null &&
          value.world_y != null
        ) {
          const position = {
            x: value.world_x,
            y: value.world_y,
            z: value.world_z ?? 0,
          };
          setBedroom(position);
          setNotice((previous) =>
            previous.kind === "saving" || previous.kind === "draft"
              ? previous
              : { kind: "current", position },
          );
        } else {
          setBedroom(null);
          setNotice((previous) =>
            previous.kind === "saving" || previous.kind === "draft"
              ? previous
              : { kind: value ? "aligning" : "unmarked" },
          );
        }
      } catch {
        if (!active) return;
        setBridgeConnected(false);
        setNotice((previous) =>
          previous.kind === "saving" || previous.kind === "draft"
            ? previous
            : { kind: "offline" },
        );
      }
    };
    void refreshBedroom();
    const timer = window.setInterval(refreshBedroom, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
      setFps(sceneRef.current?.getFps() ?? 0);
    }, 500);
    return () => window.clearInterval(timer);
  }, []);

  const setViewMode = (mode: ViewMode) => {
    setViewModeState(mode);
    sceneRef.current?.setViewMode(mode);
  };

  const beginMapPick = () => {
    if (selectingBedroom) {
      cancelDraft();
      return;
    }
    setBedroomDraft(null);
    setDraftAtRobot(false);
    setSelectingBedroom(true);
  };

  const draftRobotPosition = () => {
    const pose = sceneRef.current?.getRobotPose() ?? robotPose;
    if (!pose) {
      setNotice({ kind: "no-pose" });
      return;
    }
    setSelectingBedroom(false);
    setDraftAtRobot(true);
    setBedroomDraft(pose);
    setNotice({ kind: "draft", position: pose });
  };

  const handleMapClick = (event: MouseEvent<HTMLCanvasElement>) => {
    if (!selectingBedroom) return;
    const position = sceneRef.current?.pickGround(event.clientX, event.clientY);
    if (!position) {
      setNotice({ kind: "pick-failed" });
      return;
    }
    setSelectingBedroom(false);
    setDraftAtRobot(false);
    setBedroomDraft(position);
    setNotice({ kind: "draft", position });
  };

  const cancelDraft = () => {
    setSelectingBedroom(false);
    setBedroomDraft(null);
    setDraftAtRobot(false);
    setNotice(
      bedroom
        ? { kind: "current", position: bedroom }
        : { kind: bridgeConnected ? "unmarked" : "offline" },
    );
  };

  const saveBedroom = async () => {
    if (!bedroomDraft) return;
    const candidate = bedroomDraft;
    setSavingBedroom(true);
    setNotice({ kind: "saving" });
    try {
      const response = await fetch(`${API_BASE}/api/robot/bedroom`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          draftAtRobot
            ? { at_robot: true }
            : {
                at_robot: false,
                world_x: candidate.x,
                world_y: candidate.y,
                world_z: candidate.z,
              },
        ),
      });
      const data = (await response.json().catch(() => ({}))) as {
        detail?: string;
      };
      if (!response.ok) throw new Error(data.detail ?? c.rejected);
      setBedroom(candidate);
      setBedroomDraft(null);
      setDraftAtRobot(false);
      setNotice({ kind: "updated", position: candidate });
    } catch (error) {
      setNotice({
        kind: "error",
        detail: error instanceof Error ? error.message : c.unknownError,
      });
    } finally {
      setSavingBedroom(false);
    }
  };

  const focusRobot = () => {
    if (!sceneRef.current?.focusRobot()) {
      setNotice({ kind: "no-pose" });
      return;
    }
    setViewModeState("orbit");
  };

  const noticeText = (() => {
    switch (notice.kind) {
      case "current":
        return `${c.current} · ${formatPosition(notice.position)}`;
      case "updated":
        return `${c.updated} · ${formatPosition(notice.position)}`;
      case "draft":
        return `${c.draft} · ${formatPosition(notice.position)}`;
      case "error":
        return `${c.error} · ${notice.detail}`;
      case "unmarked":
        return c.unmarked;
      case "aligning":
        return c.aligning;
      case "offline":
        return c.offline;
      case "saving":
        return c.saving;
      case "no-pose":
        return c.noPose;
      case "pick-failed":
        return c.pickFailed;
    }
  })();

  const statusLabel = {
    live: c.live,
    "waiting-robot": c.waitingRobot,
    connecting: c.connecting,
    disconnected: c.disconnected,
  }[hud.connState];
  const cloudAge =
    hud.lastCloudAt === null ? null : Math.max(0, (now - hud.lastCloudAt) / 1000);
  const scanAge =
    hud.lastScanAt === null ? null : Math.max(0, (now - hud.lastScanAt) / 1000);
  const ageLabel = (age: number | null) =>
    age === null ? c.noData : age < 0.6 ? c.justNow : `${age.toFixed(1)} ${c.secondsAgo}`;
  const buttonClass =
    "inline-flex min-h-9 items-center justify-center border border-[#8e9aa2] bg-white px-3 font-mono text-[10px] font-bold tracking-[0.08em] text-[#26343d] transition hover:border-[#17232b] hover:bg-[#17232b] hover:text-white disabled:cursor-not-allowed disabled:opacity-35";

  if (embedded) {
    return (
      <div className="relative h-dvh min-h-0 overflow-hidden bg-[#04060c]">
        <canvas
          ref={canvasRef}
          className="block h-full w-full cursor-grab active:cursor-grabbing"
          aria-label={c.viewTitle}
        />
        <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex items-center justify-between border-b border-[#31414b] bg-[#10171c]/92 px-2 py-1.5 font-mono text-[9px] text-white">
          <span>▣ {c.mapView}</span>
          <span className={`border px-1.5 py-0.5 ${STATUS_CLASSES[hud.connState]}`}>
            ● {statusLabel}
          </span>
        </div>
        <div className="pointer-events-none absolute left-2 top-9 z-10 flex gap-1">
          {(["orbit", "top"] as ViewMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => setViewMode(mode)}
              className={`pointer-events-auto min-h-7 border border-[#50616c] bg-[#111a20]/90 px-2 font-mono text-[9px] font-bold text-white ${
                viewMode === mode ? "!border-cyan-300 !bg-cyan-300 !text-[#071015]" : ""
              }`}
            >
              {mode === "orbit" ? c.orbit : c.top}
            </button>
          ))}
        </div>
        <div className="pointer-events-none absolute inset-x-2 bottom-2 z-10 flex items-end justify-between gap-2">
          <div className="border border-cyan-800 bg-[#071015]/88 px-2 py-1 font-mono text-[9px] font-bold text-cyan-200 backdrop-blur">
            {c.mapPoints} {hud.pointCount.toLocaleString()} · {c.fps} {fps.toFixed(0)}
          </div>
          <a
            href={`/lidar?lang=${language}`}
            target="_top"
            className="pointer-events-auto border border-yellow-300 bg-yellow-300 px-2 py-1 font-mono text-[9px] font-black text-[#17130a]"
          >
            ↗ {c.openFull}
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="grid min-h-dvh grid-cols-1 grid-rows-[48px_minmax(58vh,1fr)_auto] bg-[#dfe5e8] text-[#1b262d] xl:h-dvh xl:grid-cols-[220px_minmax(0,1fr)_310px] xl:grid-rows-[48px_minmax(0,1fr)_30px]">
      <header className="col-span-full flex items-center justify-between border-b border-[#9aa6ad] bg-[#edf1f3] px-3 shadow-sm">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-8 w-8 place-items-center border border-[#7d8a92] bg-[#17232b] font-mono text-xs font-black text-[#67e8f9]">
            NW
          </span>
          <div className="min-w-0">
            <div className="truncate text-xs font-semibold">{c.app}</div>
            <div className="font-mono text-[9px] tracking-[0.14em] text-[#65747d]">
              {c.viewTitle}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div
            className="flex border border-[#9aa6ad] bg-white"
            role="group"
            aria-label={c.language}
          >
            <button
              type="button"
              onClick={() => chooseLanguage("zh")}
              className={`px-2.5 py-1 font-mono text-[10px] font-bold ${
                language === "zh" ? "bg-[#2446e8] text-white" : ""
              }`}
              aria-pressed={language === "zh"}
            >
              中文
            </button>
            <button
              type="button"
              onClick={() => chooseLanguage("en")}
              className={`px-2.5 py-1 font-mono text-[10px] font-bold ${
                language === "en" ? "bg-[#2446e8] text-white" : ""
              }`}
              aria-pressed={language === "en"}
            >
              EN
            </button>
          </div>
          <a href={OPERATOR_URL} className={buttonClass}>
            ← {c.operator}
          </a>
        </div>
      </header>

      <aside className="hidden min-h-0 overflow-y-auto border-r border-[#9aa6ad] bg-[#f3f5f6] xl:block">
        <div className="border-b border-[#aab4ba] px-3 py-3">
          <div className="font-mono text-[10px] font-black tracking-[0.12em]">
            {c.mapSource}
          </div>
          <p className="mt-1 text-[10px] leading-4 text-[#65747d]">
            {c.mapSourceHint}
          </p>
        </div>
        <div className="space-y-1 p-2 font-mono text-[10px]">
          <div className="flex items-center justify-between border border-[#b6c0c5] bg-white p-2">
            <span>◉ {c.accumulated}</span>
            <b>{hud.pointCount.toLocaleString()} {c.points}</b>
          </div>
          <div className="flex items-center justify-between border border-[#b6c0c5] bg-white p-2">
            <span className="text-amber-700">◉ {c.savedMap}</span>
            <b>{hud.premapCount.toLocaleString()} {c.points}</b>
          </div>
          <div className="flex items-center justify-between border border-[#b6c0c5] bg-white p-2">
            <span className="text-cyan-700">◉ {c.liveScan}</span>
            <b>{hud.scanCount.toLocaleString()} {c.points}</b>
          </div>
          <div className="border border-[#b6c0c5] bg-white p-2">
            <div className="flex items-center justify-between">
              <span className="text-emerald-700">◆ {c.robotPose}</span>
              <b>{robotPose ? "OK" : "—"}</b>
            </div>
            <div className="mt-2 break-words text-[9px] text-[#65747d]">
              {formatPosition(robotPose)}
            </div>
          </div>
        </div>
        <div className="border-y border-[#aab4ba] px-3 py-2 font-mono text-[9px] font-bold tracking-[0.12em] text-[#65747d]">
          {c.streamStatus}
        </div>
        <dl className="grid grid-cols-[1fr_auto] gap-x-2 gap-y-2 p-3 font-mono text-[10px]">
          <dt>{c.lastFrame}</dt>
          <dd>{ageLabel(cloudAge)}</dd>
          <dt>{c.liveScan}</dt>
          <dd>{ageLabel(scanAge)}</dd>
          <dt>{c.savedMap}</dt>
          <dd>
            {hud.premapAligned === true
              ? c.aligned
              : hud.premapAligned === false
                ? c.reference
                : c.waiting}
          </dd>
          <dt>{c.sequence}</dt>
          <dd>{hud.seq}</dd>
          <dt>{c.fps}</dt>
          <dd>{fps.toFixed(0)}</dd>
        </dl>
      </aside>

      <section className="relative min-h-0 overflow-hidden bg-[#04060c] xl:col-start-2">
        <div className="absolute inset-x-0 top-0 z-10 flex h-9 items-center justify-between border-b border-[#31414b] bg-[#10171c]/95 px-3 font-mono text-[10px] text-white">
          <span>▣ {c.mapView}</span>
          <span
            className={`border px-2 py-1 font-bold tracking-[0.1em] ${STATUS_CLASSES[hud.connState]}`}
          >
            ● {statusLabel}
          </span>
        </div>
        <canvas
          ref={canvasRef}
          className={`block h-full min-h-[58vh] w-full ${
            selectingBedroom ? "cursor-crosshair" : "cursor-grab active:cursor-grabbing"
          }`}
          onClick={handleMapClick}
          aria-label={c.viewTitle}
        />

        <div className="pointer-events-none absolute left-3 top-12 z-10 flex max-w-[calc(100%-1.5rem)] flex-wrap gap-1.5">
          {(["orbit", "top", "pov"] as ViewMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => setViewMode(mode)}
              className={`${buttonClass} pointer-events-auto border-[#50616c] bg-[#111a20]/90 text-white ${
                viewMode === mode ? "!border-cyan-300 !bg-cyan-300 !text-[#071015]" : ""
              }`}
            >
              {mode === "orbit" ? c.orbit : mode === "top" ? c.top : c.robotEyes}
            </button>
          ))}
          <button
            type="button"
            onClick={() => {
              sceneRef.current?.resetView();
              setViewModeState("orbit");
            }}
            className={`${buttonClass} pointer-events-auto border-[#50616c] bg-[#111a20]/90 text-white`}
          >
            {c.fitMap}
          </button>
          <button
            type="button"
            onClick={focusRobot}
            className={`${buttonClass} pointer-events-auto border-[#50616c] bg-[#111a20]/90 text-white`}
          >
            {c.findRobot}
          </button>
          <button
            type="button"
            onClick={() => setCamVisible((value) => !value)}
            className={`${buttonClass} pointer-events-auto border-[#50616c] bg-[#111a20]/90 text-white ${
              camVisible ? "!border-cyan-300 !text-cyan-200" : ""
            }`}
            aria-pressed={camVisible}
          >
            {c.camera}
          </button>
        </div>

        <div
          className={`pointer-events-none absolute bottom-3 left-1/2 z-10 max-w-[calc(100%-1.5rem)] -translate-x-1/2 border px-3 py-2 text-center font-mono text-[10px] font-bold backdrop-blur ${
            selectingBedroom
              ? "border-yellow-300 bg-yellow-300 text-[#17130a]"
              : "border-cyan-800 bg-[#071015]/85 text-cyan-200"
          }`}
        >
          {selectingBedroom ? c.selectHint : noticeText}
        </div>

        {camVisible && (
          <RobotCamFeed
            className="absolute bottom-12 right-3 z-10 w-72 max-w-[38vw]"
            title={c.cameraTitle}
            noSignalLabel={c.cameraOffline}
            alt={c.cameraAlt}
          />
        )}
      </section>

      <aside className="min-h-0 overflow-y-auto border-t border-[#9aa6ad] bg-[#f3f5f6] xl:col-start-3 xl:row-start-2 xl:border-l xl:border-t-0">
        <div className="border-b border-[#aab4ba] bg-[#17232b] px-4 py-3 text-white">
          <div className="font-mono text-[11px] font-black tracking-[0.12em]">
            {c.calibration}
          </div>
          <p className="mt-2 text-[10px] leading-4 text-[#c7d1d7]">
            {c.calibrationHint}
          </p>
        </div>
        <div className="space-y-3 p-3">
          <div
            className={`border px-3 py-2 font-mono text-[9px] font-bold tracking-[0.1em] ${
              bridgeConnected
                ? "border-emerald-600 bg-emerald-50 text-emerald-800"
                : "border-amber-600 bg-amber-50 text-amber-800"
            }`}
          >
            ● {bridgeConnected ? c.bridgeOnline : c.bridgeOffline}
          </div>

          <div className="border border-[#aab4ba] bg-white p-3">
            <div className="font-mono text-[9px] font-black tracking-[0.12em] text-[#65747d]">
              {c.stored}
            </div>
            <div className="mt-2 font-mono text-[11px] font-bold text-pink-700">
              {bedroom ? "BEDROOM" : c.notStored}
            </div>
            <div className="mt-1 font-mono text-[9px] text-[#65747d]">
              {formatPosition(bedroom)}
            </div>
          </div>

          <div className="border border-[#aab4ba] bg-white p-3">
            <div className="font-mono text-[9px] font-black tracking-[0.12em] text-[#65747d]">
              {c.robot}
            </div>
            <div className="mt-2 font-mono text-[9px]">
              {robotPose ? formatPosition(robotPose) : c.unavailable}
            </div>
          </div>

          <div className="grid gap-2">
            <button
              type="button"
              onClick={beginMapPick}
              className={`${buttonClass} ${
                selectingBedroom ? "!border-yellow-600 !bg-yellow-300 !text-[#17130a]" : ""
              }`}
            >
              {selectingBedroom ? c.selecting : c.markAnywhere}
            </button>
            <button
              type="button"
              onClick={draftRobotPosition}
              className={buttonClass}
            >
              {c.markRobot}
            </button>
          </div>

          {bedroomDraft && (
            <div className="border-2 border-yellow-500 bg-yellow-50 p-3">
              <div className="font-mono text-[9px] font-black tracking-[0.12em] text-yellow-900">
                {c.draftTitle}
              </div>
              <div className="mt-2 font-mono text-[10px] font-bold">
                {formatPosition(bedroomDraft)}
              </div>
              <p className="mt-2 text-[10px] leading-4 text-yellow-900">
                {c.replaceHint}
              </p>
              <div className="mt-3 grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => void saveBedroom()}
                  disabled={savingBedroom}
                  className={`${buttonClass} !border-[#2446e8] !bg-[#2446e8] !text-white`}
                >
                  {c.confirm}
                </button>
                <button
                  type="button"
                  onClick={cancelDraft}
                  disabled={savingBedroom}
                  className={buttonClass}
                >
                  {c.cancel}
                </button>
              </div>
            </div>
          )}

          <div
            className={`border px-3 py-2 font-mono text-[9px] leading-4 ${
              notice.kind === "error" || notice.kind === "pick-failed"
                ? "border-red-500 bg-red-50 text-red-800"
                : "border-[#aab4ba] bg-white text-[#4d5c65]"
            }`}
          >
            {noticeText}
          </div>

          <ol className="list-decimal space-y-2 border-t border-[#aab4ba] px-4 pt-3 text-[10px] leading-4 text-[#56656e]">
            <li>{c.step1}</li>
            <li>{c.step2}</li>
            <li>{c.step3}</li>
          </ol>
        </div>
      </aside>

      <footer className="col-span-full hidden items-center justify-between border-t border-[#9aa6ad] bg-[#edf1f3] px-3 font-mono text-[9px] text-[#65747d] xl:flex">
        <span>{c.canvasHint}</span>
        <span>
          {c.sequence} {hud.seq} · {c.fps} {fps.toFixed(0)}
        </span>
      </footer>
    </div>
  );
}
