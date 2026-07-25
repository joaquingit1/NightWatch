"use client";

import { useEffect, useRef, useState } from "react";

import {
  type CloudFrame,
  type PremapMessage,
  type PoseMessage,
  parseCloudFrame,
  parseTextMessage,
} from "./protocol";

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 8000;

export type ConnState = "connecting" | "live" | "waiting-robot" | "disconnected";

export interface LidarSocketCallbacks {
  onCloud: (frame: CloudFrame) => void;
  onPremap: (frame: CloudFrame) => void;
  onScan: (frame: CloudFrame) => void;
  onPose: (pose: PoseMessage) => void;
  onPremapStatus?: (status: PremapMessage) => void;
}

export interface LidarHudState {
  connState: ConnState;
  pointCount: number;
  seq: number;
  lastCloudAt: number | null;
  scanCount: number;
  lastScanAt: number | null;
  premapCount: number;
  premapAligned: boolean | null;
}

// Same reasoning as the SSE connection in ThoughtTicker: Next.js rewrites
// cannot proxy WebSockets, so the browser talks to FastAPI directly.
function buildWsUrl(): string {
  const base = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
  return base.replace(/^http/, "ws") + "/ws/lidar";
}

export function useLidarSocket(callbacks: LidarSocketCallbacks): LidarHudState {
  const callbacksRef = useRef(callbacks);
  callbacksRef.current = callbacks;

  const [hud, setHud] = useState<LidarHudState>({
    connState: "connecting",
    pointCount: 0,
    seq: 0,
    lastCloudAt: null,
    scanCount: 0,
    lastScanAt: null,
    premapCount: 0,
    premapAligned: null,
  });

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let backoff = INITIAL_BACKOFF_MS;
    let reconnectTimer: number | undefined;

    const url = buildWsUrl();

    const connect = () => {
      if (closed) return;
      setHud((h) => ({ ...h, connState: "connecting" }));
      ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";

      ws.onopen = () => {
        backoff = INITIAL_BACKOFF_MS;
      };

      ws.onmessage = (event: MessageEvent) => {
        if (typeof event.data === "string") {
          const msg = parseTextMessage(event.data);
          if (!msg) return;
          if (msg.type === "pose") {
            callbacksRef.current.onPose(msg);
          } else if (msg.type === "premap") {
            callbacksRef.current.onPremapStatus?.(msg);
            setHud((h) => ({ ...h, premapAligned: msg.aligned }));
          } else {
            if (!msg.connected) {
              // The upstream robot stream dropped (typically a robot/stack
              // restart). The next session gets a NEW world frame, so the
              // last world_to_map must not survive the gap: a zone drawn
              // with the stale transform would be pinned to the wrong spot.
              callbacksRef.current.onPremapStatus?.({
                type: "premap",
                aligned: false,
              });
              setHud((h) => ({ ...h, premapAligned: null }));
            }
            setHud((h) => ({
              ...h,
              connState: msg.connected ? "live" : "waiting-robot",
            }));
          }
        } else if (event.data instanceof ArrayBuffer) {
          const frame = parseCloudFrame(event.data);
          if (!frame) return;
          if (frame.kind === "scan") {
            callbacksRef.current.onScan(frame);
            setHud((h) => ({
              ...h,
              connState: "live",
              scanCount: frame.count,
              lastScanAt: Date.now(),
            }));
          } else if (frame.kind === "premap") {
            callbacksRef.current.onPremap(frame);
            setHud((h) => ({
              ...h,
              connState: "live",
              premapCount: frame.count,
            }));
          } else {
            callbacksRef.current.onCloud(frame);
            setHud((h) => ({
              ...h,
              connState: "live",
              pointCount: frame.count,
              seq: frame.seq,
              lastCloudAt: Date.now(),
            }));
          }
        }
      };

      ws.onclose = () => {
        if (closed) return;
        // Same reasoning as the upstream-disconnected status: never keep a
        // world->map transform across a connection gap. The current session's
        // status (with its transform, if aligned) is replayed on reconnect.
        callbacksRef.current.onPremapStatus?.({
          type: "premap",
          aligned: false,
        });
        setHud((h) => ({ ...h, connState: "disconnected", premapAligned: null }));
        reconnectTimer = window.setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      };

      ws.onerror = () => {
        ws?.close();
      };
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);

  return hud;
}
