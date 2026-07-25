"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import type { CloudFrame, PoseMessage } from "./protocol";
import { createLidarScene, type LidarScene, type ViewMode } from "./scene";
import { type ConnState, useLidarSocket } from "./useLidarSocket";
import {
  type MapPoint,
  type MapZone,
  type WorldToMap,
  type ZoneKind,
  worldToMapPoint,
  zoneWorldPoints,
} from "./zones";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

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
  const [zones, setZones] = useState<MapZone[]>([]);
  const [venue, setVenue] = useState("legacy");
  const [zoneMode, setZoneMode] = useState<ZoneKind | null>(null);
  const [draftPoints, setDraftPoints] = useState<MapPoint[]>([]);
  const [worldToMap, setWorldToMap] = useState<WorldToMap | null>(null);
  const [zoneBusy, setZoneBusy] = useState(false);
  const [zoneError, setZoneError] = useState<string | null>(null);
  const [controlVisible, setControlVisible] = useState(false);
  const [robot, setRobot] = useState<{
    enabled?: boolean;
    connected?: boolean;
    behavior?: string;
    operating_mode?: "autonomous" | "sleep_analysis" | "manual";
    interaction_state?: string;
    sleeping_areas?: number;
    sleeping_area_list?: {
      area_id: string;
      x: number;
      y: number;
      anchored: boolean;
      protected: boolean;
      label: string;
    }[];
    battery_soc?: number | null;
    hold_reason?: string | null;
    home_arrived?: boolean;
    returning_home?: boolean;
    return_route_blocked?: boolean;
  } | null>(null);
  const [commandBusy, setCommandBusy] = useState(false);
  const [commandMessage, setCommandMessage] = useState("");

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
    onPremapStatus: (premap) => {
      setWorldToMap(premap.world_to_map ?? null);
    },
  });

  const loadZones = async () => {
    try {
      const response = await fetch(`${API_BASE}/api/zones`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`zones unavailable (${response.status})`);
      const payload = (await response.json()) as {
        venue?: string;
        zones: MapZone[];
      };
      setZones(payload.zones);
      setVenue(payload.venue ?? "legacy");
      setZoneError(null);
    } catch (error) {
      setZoneError(error instanceof Error ? error.message : "zones unavailable");
    }
  };

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
    void loadZones();
  }, []);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/robot/status`, {
          cache: "no-store",
        });
        if (active && response.ok) setRobot(await response.json());
      } catch {
        // The console remains visible and retries when the robot reconnects.
      }
    };
    void poll();
    const timer = window.setInterval(poll, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    // Escort destinations from the robot's world model, drawn in the shared
    // world frame like zones (self-tagged, MARK SLEEP button, sleep-zone
    // polygons, and AprilTag areas all appear here).
    sceneRef.current?.setSleepingAreas(
      (robot?.sleeping_area_list ?? []).map((area) => ({
        x: area.x,
        y: area.y,
        anchored: area.anchored,
      }))
    );
  }, [robot?.sleeping_area_list]);

  const sendRobotAction = async (
    action:
      | "hold"
      | "resume"
      | "wave"
      | "lie_down"
      | "stand_up"
      | "set_home"
      | "return_home"
      | "mark_sleep"
      | "mode_autonomous"
      | "mode_sleep"
      | "scan_now"
  ) => {
    if (commandBusy) return;
    setCommandBusy(true);
    setCommandMessage("");
    try {
      const response = await fetch(`${API_BASE}/api/robot/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const payload = (await response.json()) as {
        ok: boolean;
        message: string;
      };
      setCommandMessage(payload.message);
    } catch {
      setCommandMessage("Robot command failed");
    } finally {
      setCommandBusy(false);
    }
  };

  const setNavigationMode = async (mode: "focus" | "full") => {
    setZoneBusy(true);
    setZoneError(null);
    try {
      const response = await fetch(`${API_BASE}/api/zones/mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!response.ok) throw new Error(`mode change failed (${response.status})`);
      await loadZones();
    } catch (error) {
      setZoneError(error instanceof Error ? error.message : "mode change failed");
    } finally {
      setZoneBusy(false);
    }
  };

  const returnToBoothAndRestrict = async () => {
    if (commandBusy) return;
    setCommandBusy(true);
    setCommandMessage("Returning to focus area…");
    try {
      const recall = await fetch(`${API_BASE}/api/robot/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "return_home" }),
      });
      const recallResult = (await recall.json()) as {
        ok: boolean;
        message: string;
      };
      if (!recallResult.ok) throw new Error(recallResult.message);

      const deadline = Date.now() + 120_000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const response = await fetch(`${API_BASE}/api/robot/status`, {
          cache: "no-store",
        });
        if (!response.ok) continue;
        const status = (await response.json()) as {
          home_arrived?: boolean;
          return_route_blocked?: boolean;
        };
        if (status.return_route_blocked) {
          throw new Error("Return route blocked; robot is holding safely");
        }
        if (!status.home_arrived) continue;

        await fetch(`${API_BASE}/api/zones/mode`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: "focus" }),
        });
        await fetch(`${API_BASE}/api/robot/action`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "resume" }),
        });
        await loadZones();
        setCommandMessage("Inside focus area · boundary active · autonomy resumed");
        return;
      }
      throw new Error("Return timed out; robot remains in its current safety state");
    } catch (error) {
      setCommandMessage(
        error instanceof Error ? error.message : "Return to booth failed"
      );
    } finally {
      setCommandBusy(false);
    }
  };

  useEffect(() => {
    const displayZones = zones
      .filter((zone) => zone.active !== false)
      .map((zone) => ({
        ...zone,
        world_points: zoneWorldPoints(zone, worldToMap),
      }));
    sceneRef.current?.setZones(displayZones);
  }, [zones, worldToMap]);

  useEffect(() => {
    sceneRef.current?.setDrawing(zoneMode !== null);
    sceneRef.current?.setDraftZone(zoneMode, draftPoints);
  }, [zoneMode, draftPoints]);

  const startZone = (kind: ZoneKind) => {
    setZoneMode(kind);
    setDraftPoints([]);
    setViewMode("orbit");
    sceneRef.current?.setViewMode("orbit");
    setZoneError(null);
  };

  const cancelZone = () => {
    setZoneMode(null);
    setDraftPoints([]);
  };

  const saveZone = async () => {
    if (!zoneMode || draftPoints.length < 3 || zoneBusy) return;
    setZoneBusy(true);
    try {
      const response = await fetch(`${API_BASE}/api/zones`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: zoneMode,
          name:
            zoneMode === "keep_in"
              ? "Focus area"
              : zoneMode === "sleeping"
                ? "Sleeping area"
                : "Restricted area",
          points_frame: worldToMap ? "map" : "world",
          points: draftPoints.map((point) =>
            worldToMapPoint(point, worldToMap)
          ),
          world_points: draftPoints,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail ?? `save failed (${response.status})`);
      }
      cancelZone();
      await loadZones();
    } catch (error) {
      setZoneError(error instanceof Error ? error.message : "could not save zone");
    } finally {
      setZoneBusy(false);
    }
  };

  const deleteZone = async (zoneId: string) => {
    if (zoneBusy) return;
    setZoneBusy(true);
    try {
      const response = await fetch(`${API_BASE}/api/zones/${zoneId}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error(`delete failed (${response.status})`);
      await loadZones();
    } catch (error) {
      setZoneError(error instanceof Error ? error.message : "could not delete zone");
    } finally {
      setZoneBusy(false);
    }
  };

  const toggleZone = async (zone: MapZone) => {
    if (zoneBusy) return;
    setZoneBusy(true);
    try {
      const response = await fetch(`${API_BASE}/api/zones/${zone.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: zone.active === false }),
      });
      if (!response.ok) throw new Error(`update failed (${response.status})`);
      await loadZones();
    } catch (error) {
      setZoneError(error instanceof Error ? error.message : "could not update zone");
    } finally {
      setZoneBusy(false);
    }
  };

  const handleCanvasClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    if (!zoneMode) return;
    const point = sceneRef.current?.screenToMapPoint(
      event.clientX,
      event.clientY
    );
    if (point) setDraftPoints((current) => [...current, point]);
  };

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
      setFps(sceneRef.current?.getFps() ?? 0);
    }, 500);
    return () => window.clearInterval(timer);
  }, []);

  const status = STATUS_STYLES[hud.connState];
  const activeZoneCount = zones.filter((zone) => zone.active !== false).length;
  const focusAreaActive = zones.some(
    (zone) => zone.kind === "keep_in" && zone.active !== false
  );
  const cloudAge =
    hud.lastCloudAt === null ? null : Math.max(0, (now - hud.lastCloudAt) / 1000);
  const scanAge =
    hud.lastScanAt === null ? null : Math.max(0, (now - hud.lastScanAt) / 1000);

  return (
    <div className="relative h-full w-full">
      <canvas
        ref={canvasRef}
        onClick={handleCanvasClick}
        className={`block h-full w-full ${
          zoneMode ? "cursor-crosshair" : "cursor-grab"
        }`}
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
              onClick={() => setControlVisible((value) => !value)}
              className={`pointer-events-auto rounded-md border bg-black/40 px-3 py-1.5 backdrop-blur transition-colors ${
                controlVisible
                  ? "border-amber-400/50 text-amber-200"
                  : "border-cyan-500/20 text-cyan-300/70 hover:border-cyan-400/50 hover:text-cyan-200"
              }`}
            >
              CONTROL
            </button>
            <Link
              href="/first-person"
              className="pointer-events-auto rounded-md border border-cyan-500/20 bg-black/40 px-3 py-1.5 text-cyan-300/70 backdrop-blur transition-colors hover:border-cyan-400/50 hover:text-cyan-200"
            >
              FIRST PERSON
            </Link>
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
              &larr; ANALYSIS
            </Link>
          </div>
        </div>

        {controlVisible && (
          <section className="pointer-events-auto absolute right-4 top-14 w-80 border border-white/15 bg-[#070a10]/95 text-white shadow-2xl backdrop-blur">
            <header className="flex items-center justify-between border-b border-white/10 px-3 py-2">
              <div>
                <div className="text-[9px] tracking-[0.18em] text-white/45">
                  COMMAND &amp; CONTROL
                </div>
                <div className="mt-1 flex items-center gap-2 text-[10px] text-white/75">
                  <span
                    className={`h-2 w-2 rounded-full ${
                      robot?.connected ? "bg-emerald-400" : "bg-red-400"
                    }`}
                  />
                  {robot?.connected ? "ROBOT ONLINE" : "ROBOT OFFLINE"}
                  {robot?.battery_soc != null &&
                    ` · ${robot.battery_soc.toFixed(0)}%`}
                  {robot?.sleeping_areas != null &&
                    ` · ${robot.sleeping_areas} REST AREAS`}
                </div>
              </div>
              <span className="text-[9px] uppercase text-cyan-300/70">
                {robot?.behavior ?? "waiting"}
              </span>
            </header>
            {robot?.hold_reason && (
              <div className="border-b border-amber-400/20 bg-amber-400/5 px-3 py-2 text-[9px] text-amber-300">
                HOLD / {robot.hold_reason}
              </div>
            )}
            <div className="grid grid-cols-2 gap-2 p-3">
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("mode_autonomous")}
                className={`border px-2 py-2 text-[10px] disabled:opacity-30 ${
                  robot?.operating_mode === "autonomous"
                    ? "border-emerald-300 bg-emerald-400/20 text-emerald-100"
                    : "border-white/20 text-white/70"
                }`}
              >
                AUTONOMOUS
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("mode_sleep")}
                className={`border px-2 py-2 text-[10px] disabled:opacity-30 ${
                  robot?.operating_mode === "sleep_analysis"
                    ? "border-violet-300 bg-violet-400/20 text-violet-100"
                    : "border-white/20 text-white/70"
                }`}
              >
                SLEEP ANALYSIS
              </button>
              <button
                type="button"
                disabled={
                  !robot?.connected ||
                  commandBusy ||
                  robot?.operating_mode !== "sleep_analysis"
                }
                onClick={() => void sendRobotAction("scan_now")}
                className="border border-violet-400/40 bg-violet-400/10 px-2 py-2 text-[10px] text-violet-200 disabled:opacity-30"
              >
                SCAN NOW
              </button>
              <a
                href="http://localhost:5555/operator"
                className="flex min-h-9 items-center justify-center border border-red-400/40 bg-red-400/10 px-2 py-2 text-center text-[10px] text-red-200"
              >
                MANUAL CONSOLE
              </a>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("wave")}
                className="border border-cyan-400/40 bg-cyan-400/10 px-2 py-2 text-[10px] text-cyan-200 disabled:opacity-30"
              >
                WAVE HELLO
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("hold")}
                className="border border-amber-400/40 px-2 py-2 text-[10px] text-amber-200 disabled:opacity-30"
              >
                PAUSE / HOLD
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("lie_down")}
                className="border border-red-400/40 px-2 py-2 text-[10px] text-red-200 disabled:opacity-30"
              >
                LIE DOWN
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("stand_up")}
                className="border border-emerald-400/40 bg-emerald-400/10 px-2 py-2 text-[10px] text-emerald-200 disabled:opacity-30"
              >
                STAND / RESUME
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("resume")}
                className="col-span-2 border border-white/20 px-2 py-2 text-[10px] text-white/70 disabled:opacity-30"
              >
                RESUME AUTONOMY
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("set_home")}
                className="border border-white/20 px-2 py-2 text-[10px] text-white/70 disabled:opacity-30"
              >
                SET HOME BASE
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void returnToBoothAndRestrict()}
                className="border border-amber-400/50 bg-amber-400/10 px-2 py-2 text-[10px] text-amber-200 disabled:opacity-30"
              >
                RETURN TO FOCUS
              </button>
              <button
                type="button"
                disabled={!robot?.connected || commandBusy}
                onClick={() => void sendRobotAction("mark_sleep")}
                className="col-span-2 border border-violet-400/40 bg-violet-400/10 px-2 py-2 text-[10px] text-violet-200 disabled:opacity-30"
              >
                MARK SLEEP AREA HERE
              </button>
            </div>
            {commandMessage && (
              <div className="border-t border-white/10 px-3 py-2 text-[9px] text-white/55">
                RESPONSE / {commandMessage}
              </div>
            )}
          </section>
        )}

        <div className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full border border-cyan-500/15 bg-black/40 px-4 py-1.5 text-cyan-300/50 backdrop-blur">
          {zoneMode
            ? `click map to add vertices · ${draftPoints.length} placed`
            : viewMode === "pov"
            ? "first-person view from the robot"
            : "drag to orbit · scroll to zoom · right-drag to pan"}
        </div>

        <section className="pointer-events-auto absolute bottom-4 left-4 w-72 border border-white/15 bg-[#070a10]/90 text-white shadow-2xl backdrop-blur">
          <header className="flex items-center justify-between border-b border-white/10 px-3 py-2">
            <div>
              <div className="text-[9px] tracking-[0.18em] text-white/45">
                NAVIGATION LIMITS / {venue.toUpperCase()}
              </div>
              <div className="mt-0.5 text-xs text-white/80">
                {activeZoneCount} active zone{activeZoneCount === 1 ? "" : "s"}
              </div>
            </div>
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
          </header>

          <div className="grid grid-cols-3 gap-2 p-3">
            <button
              type="button"
              onClick={() => startZone("keep_in")}
              className={`border px-2 py-2 text-[10px] tracking-wider transition ${
                zoneMode === "keep_in"
                  ? "border-emerald-300 bg-emerald-400/20 text-emerald-200"
                  : "border-emerald-400/35 text-emerald-300 hover:bg-emerald-400/10"
              }`}
            >
              + FOCUS
            </button>
            <button
              type="button"
              onClick={() => startZone("keep_out")}
              className={`border px-2 py-2 text-[10px] tracking-wider transition ${
                zoneMode === "keep_out"
                  ? "border-red-300 bg-red-400/20 text-red-200"
                  : "border-red-400/35 text-red-300 hover:bg-red-400/10"
              }`}
            >
              + KEEP OUT
            </button>
            <button
              type="button"
              onClick={() => startZone("sleeping")}
              className={`border px-2 py-2 text-[10px] tracking-wider transition ${
                zoneMode === "sleeping"
                  ? "border-sky-300 bg-sky-400/20 text-sky-200"
                  : "border-sky-400/35 text-sky-300 hover:bg-sky-400/10"
              }`}
            >
              + SLEEP ZONE
            </button>
          </div>

          <div className="grid grid-cols-2 gap-2 border-t border-white/10 px-3 py-3">
            <button
              type="button"
              disabled={zoneBusy}
              onClick={() => void setNavigationMode("full")}
              className={`border px-2 py-2 text-[9px] disabled:opacity-30 ${
                !focusAreaActive
                  ? "border-cyan-300 bg-cyan-400/20 text-cyan-100"
                  : "border-cyan-400/30 text-cyan-300"
              }`}
            >
              FULL VENUE
            </button>
            <button
              type="button"
              disabled={zoneBusy}
              onClick={() => void setNavigationMode("focus")}
              className={`border px-2 py-2 text-[9px] disabled:opacity-30 ${
                focusAreaActive
                  ? "border-emerald-300 bg-emerald-400/20 text-emerald-100"
                  : "border-emerald-400/30 text-emerald-300"
              }`}
            >
              FOCUS AREA
            </button>
            <p className="col-span-2 text-[8px] leading-4 text-white/40">
              Full venue removes the allowed-area boundary while every hazard
              remains enforced. Return to focus recalls the dog before restoring
              the selected operating area.
            </p>
          </div>

          {zoneMode && (
            <div className="mx-3 mb-3 border border-white/10 bg-white/[0.04] p-2">
              <div className="mb-2 flex items-center justify-between text-[10px]">
                <span
                  className={
                    zoneMode === "keep_in"
                      ? "text-emerald-300"
                      : zoneMode === "sleeping"
                        ? "text-sky-300"
                        : "text-red-300"
                  }
                >
                  DRAWING{" "}
                  {zoneMode === "keep_in"
                    ? "ALLOWED AREA"
                    : zoneMode === "sleeping"
                      ? "SLEEPING AREA"
                      : "KEEP OUT"}
                </span>
                <span className="text-white/45">{draftPoints.length} vertices</span>
              </div>
              <div className="grid grid-cols-3 gap-1">
                <button
                  type="button"
                  onClick={() => setDraftPoints((points) => points.slice(0, -1))}
                  disabled={draftPoints.length === 0}
                  className="border border-white/15 px-2 py-1.5 text-[9px] text-white/65 disabled:opacity-30"
                >
                  UNDO
                </button>
                <button
                  type="button"
                  onClick={cancelZone}
                  className="border border-white/15 px-2 py-1.5 text-[9px] text-white/65"
                >
                  CANCEL
                </button>
                <button
                  type="button"
                  onClick={() => void saveZone()}
                  disabled={draftPoints.length < 3 || zoneBusy}
                  className="border border-cyan-400/50 bg-cyan-400/10 px-2 py-1.5 text-[9px] text-cyan-200 disabled:opacity-30"
                >
                  SAVE
                </button>
              </div>
            </div>
          )}

          {zones.length > 0 && (
            <div className="max-h-28 overflow-y-auto border-t border-white/10">
              {zones.map((zone) => (
                <div
                  key={zone.id}
                  className={`flex items-center justify-between border-b border-white/[0.06] px-3 py-2 last:border-b-0 ${
                    zone.active === false ? "opacity-35" : ""
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span
                      className={`h-2 w-2 shrink-0 ${
                        zone.kind === "keep_in"
                          ? "bg-emerald-400"
                          : zone.kind === "sleeping"
                            ? "bg-sky-400"
                            : "bg-red-400"
                      }`}
                    />
                    <span className="truncate text-[10px] text-white/70">
                      {zone.name}
                    </span>
                  </div>
                  <div className="ml-2 flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => void toggleZone(zone)}
                      aria-label={`${zone.active === false ? "Enable" : "Disable"} ${zone.name}`}
                      className={`border px-1.5 py-1 text-[8px] ${
                        zone.active === false
                          ? "border-white/15 text-white/40"
                          : "border-emerald-400/30 text-emerald-300"
                      }`}
                    >
                      {zone.active === false ? "OFF" : "ON"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void deleteZone(zone.id)}
                      aria-label={`Delete ${zone.name}`}
                      className="px-1 text-sm text-white/35 hover:text-red-300"
                    >
                      ×
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {zoneError && (
            <div className="border-t border-red-400/20 px-3 py-2 text-[10px] text-red-300">
              {zoneError}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
