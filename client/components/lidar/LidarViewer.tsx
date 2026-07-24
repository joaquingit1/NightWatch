"use client";

import Link from "next/link";
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

const STATUS_STYLES: Record<ConnState, { label: string; className: string }> = {
  live: { label: "LIVE", className: "border-emerald-400/40 text-emerald-300" },
  "waiting-robot": {
    label: "WAITING FOR ROBOT",
    className: "border-amber-400/40 text-amber-300",
  },
  connecting: {
    label: "CONNECTING",
    className: "border-cyan-400/40 text-cyan-300",
  },
  disconnected: {
    label: "DISCONNECTED",
    className: "border-red-400/40 text-red-300",
  },
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export function LidarViewer() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const sceneRef = useRef<LidarScene | null>(null);
  // The socket can deliver frames before the scene effect has run; buffer the
  // latest of each and replay on mount.
  const pendingCloudRef = useRef<CloudFrame | null>(null);
  const pendingPremapRef = useRef<CloudFrame | null>(null);
  const pendingScanRef = useRef<CloudFrame | null>(null);
  const pendingPoseRef = useRef<PoseMessage | null>(null);

  const [fps, setFps] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [viewMode, setViewMode] = useState<ViewMode>("orbit");
  const [camVisible, setCamVisible] = useState(true);
  const [selectingBedroom, setSelectingBedroom] = useState(false);
  const [bedroom, setBedroom] = useState<WorldPosition | null>(null);
  const [bedroomStatus, setBedroomStatus] = useState("Bedroom 尚未标记");

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
      if (sceneRef.current) sceneRef.current.updatePose(pose);
      else pendingPoseRef.current = pose;
    },
  });

  const toggleViewMode = () => {
    const next: ViewMode = viewMode === "orbit" ? "pov" : "orbit";
    setViewMode(next);
    sceneRef.current?.setViewMode(next);
  };

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
    let active = true;
    const refreshBedroom = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/robot/bedroom`, {
          cache: "no-store",
        });
        if (!response.ok) throw new Error();
        const data = (await response.json()) as {
          bedroom?: {
            available?: boolean;
            world_x?: number;
            world_y?: number;
            world_z?: number;
          } | null;
        };
        const value = data.bedroom;
        if (!active) return;
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
          sceneRef.current?.setBedroom(position);
          setBedroomStatus(
            `Bedroom · (${position.x.toFixed(2)}, ${position.y.toFixed(2)})`,
          );
        } else {
          setBedroom(null);
          sceneRef.current?.setBedroom(null);
          setBedroomStatus(value ? "Bedroom 等待地图对齐" : "Bedroom 尚未标记");
        }
      } catch {
        if (active) setBedroomStatus("Bedroom 服务离线");
      }
    };
    refreshBedroom();
    const timer = window.setInterval(refreshBedroom, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const saveBedroom = async (position: WorldPosition, atRobot = false) => {
    try {
      setBedroomStatus("正在更新 Bedroom…");
      const response = await fetch(`${API_BASE}/api/robot/bedroom`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          atRobot
            ? { at_robot: true }
            : {
                at_robot: false,
                world_x: position.x,
                world_y: position.y,
                world_z: position.z,
              },
        ),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "机器人拒绝更新");
      setBedroom(position);
      sceneRef.current?.setBedroom(position);
      setBedroomStatus(
        `Bedroom 已更新 · (${position.x.toFixed(2)}, ${position.y.toFixed(2)})`,
      );
    } catch (error) {
      setBedroomStatus(
        `更新失败 · ${error instanceof Error ? error.message : "未知错误"}`,
      );
    } finally {
      setSelectingBedroom(false);
    }
  };

  const markAtRobot = () => {
    const pose = sceneRef.current?.getRobotPose();
    if (!pose) {
      setBedroomStatus("尚未收到机器人位置");
      return;
    }
    if (window.confirm("将 Bedroom 更新为机器人当前所在位置？")) {
      void saveBedroom(pose, true);
    }
  };

  const handleMapClick = (event: MouseEvent<HTMLCanvasElement>) => {
    if (!selectingBedroom) return;
    const position = sceneRef.current?.pickGround(event.clientX, event.clientY);
    if (!position) {
      setBedroomStatus("无法在该视角定位地面，请切换到俯视角再试");
      return;
    }
    if (
      window.confirm(
        `将 Bedroom 更新为 (${position.x.toFixed(2)}, ${position.y.toFixed(2)})？旧位置会被替换。`,
      )
    ) {
      void saveBedroom(position);
    } else {
      setSelectingBedroom(false);
    }
  };

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
      setFps(sceneRef.current?.getFps() ?? 0);
    }, 500);
    return () => window.clearInterval(timer);
  }, []);

  const status = STATUS_STYLES[hud.connState];
  const cloudAge =
    hud.lastCloudAt === null ? null : Math.max(0, (now - hud.lastCloudAt) / 1000);
  const scanAge =
    hud.lastScanAt === null ? null : Math.max(0, (now - hud.lastScanAt) / 1000);

  return (
    <div className="relative h-full w-full">
      <canvas
        ref={canvasRef}
        className={`block h-full w-full ${selectingBedroom ? "cursor-crosshair" : ""}`}
        onClick={handleMapClick}
      />

      <div className="pointer-events-none absolute inset-0 p-4 font-mono text-xs">
        <div className="flex items-start justify-between">
          <div className="flex flex-col gap-2">
            <span
              className={`inline-flex w-fit items-center gap-2 rounded-full border bg-black/40 px-3 py-1 tracking-widest backdrop-blur ${status.className}`}
            >
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
              {status.label}
            </span>
            <div className="w-fit rounded-md border border-cyan-500/20 bg-black/40 px-3 py-2 text-cyan-300/70 backdrop-blur">
              <div>MAP {hud.pointCount.toLocaleString()} pts</div>
              <div className="text-amber-300/70">
                SAVED {hud.premapCount.toLocaleString()} pts ·{" "}
                {hud.premapAligned === true
                  ? "aligned"
                  : hud.premapAligned === false
                    ? "reference frame"
                    : "waiting"}
              </div>
              <div>
                SCAN {hud.scanCount.toLocaleString()} pts
                {scanAge !== null && scanAge < 2 ? " · live" : ""}
              </div>
              <div>SEQ {hud.seq}</div>
              <div>
                SNAPSHOT{" "}
                {cloudAge === null ? "waiting" : `${cloudAge.toFixed(1)}s ago`}
              </div>
              <div>FPS {fps.toFixed(0)}</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setSelectingBedroom((value) => !value)}
              className={`pointer-events-auto rounded-md border bg-black/40 px-3 py-1.5 backdrop-blur transition-colors ${
                selectingBedroom
                  ? "border-pink-400/70 text-pink-200"
                  : "border-pink-500/25 text-pink-300/70 hover:border-pink-400/60"
              }`}
            >
              {selectingBedroom ? "点击地图位置…" : "标记 BEDROOM"}
            </button>
            <button
              type="button"
              onClick={markAtRobot}
              className="pointer-events-auto rounded-md border border-pink-500/25 bg-black/40 px-3 py-1.5 text-pink-300/70 backdrop-blur transition-colors hover:border-pink-400/60"
            >
              使用机器人位置
            </button>
            <button
              type="button"
              onClick={() => setCamVisible((value) => !value)}
              className={`pointer-events-auto rounded-md border bg-black/40 px-3 py-1.5 backdrop-blur transition-colors ${
                camVisible
                  ? "border-cyan-400/50 text-cyan-200"
                  : "border-cyan-500/20 text-cyan-300/50 hover:border-cyan-400/50 hover:text-cyan-200"
              }`}
              aria-pressed={camVisible}
            >
              CAM
            </button>
            <button
              type="button"
              onClick={toggleViewMode}
              className={`pointer-events-auto rounded-md border bg-black/40 px-3 py-1.5 backdrop-blur transition-colors ${
                viewMode === "pov"
                  ? "border-amber-400/50 text-amber-300 hover:border-amber-300/70"
                  : "border-cyan-500/20 text-cyan-300/70 hover:border-cyan-400/50 hover:text-cyan-200"
              }`}
            >
              {viewMode === "pov" ? "ROBOT EYES" : "ORBIT VIEW"}
            </button>
            <Link
              href="/"
              className="pointer-events-auto rounded-md border border-cyan-500/20 bg-black/40 px-3 py-1.5 text-cyan-300/70 backdrop-blur transition-colors hover:border-cyan-400/50 hover:text-cyan-200"
            >
              &larr; BOOTH
            </Link>
          </div>
        </div>

        <div className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full border border-cyan-500/15 bg-black/40 px-4 py-1.5 text-cyan-300/50 backdrop-blur">
          {selectingBedroom
            ? "点击地图任意地面位置以更新唯一 Bedroom"
            : `${bedroomStatus}${bedroom ? "" : " · 可在任意位置标记"}`}
        </div>

        {camVisible && (
          <RobotCamFeed
            className={`absolute bottom-4 right-4 ${
              viewMode === "pov" ? "w-[26rem] max-w-[45vw]" : "w-72 max-w-[35vw]"
            }`}
          />
        )}
      </div>
    </div>
  );
}
