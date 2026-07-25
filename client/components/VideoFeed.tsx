"use client";

import Hls from "hls.js";
import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const RTMP_HLS_URL =
  process.env.NEXT_PUBLIC_RTMP_HLS_URL ?? "http://localhost:8080/hls/stream.m3u8";

type VideoSourceMode = "analysis" | "raw" | "robot" | "rtmp";

const SOURCE_OPTIONS: { id: VideoSourceMode; label: string }[] = [
  { id: "analysis", label: "Analysis" },
  { id: "raw", label: "Raw" },
  { id: "robot", label: "Robot" },
  { id: "rtmp", label: "RTMP" },
];

interface VideoFeedProps {
  src?: string;
  povSrc?: string;
  annotatedSrc?: string;
  robotSrc?: string;
  rtmpSrc?: string;
  label: string;
  className?: string;
  defaultMode?: VideoSourceMode;
  /** @deprecated Prefer defaultMode="analysis" | "raw" */
  defaultShowOverlay?: boolean;
}

function resolveMjpegSrc(
  mode: Exclude<VideoSourceMode, "rtmp">,
  urls: { pov: string; annotated: string; robot: string }
): string {
  if (mode === "analysis") return urls.annotated;
  if (mode === "robot") return urls.robot;
  return urls.pov;
}

export function VideoFeed({
  src,
  povSrc,
  annotatedSrc,
  robotSrc,
  rtmpSrc,
  label,
  className = "",
  defaultMode,
  defaultShowOverlay = true,
}: VideoFeedProps) {
  const initialMode: VideoSourceMode =
    defaultMode ?? (defaultShowOverlay ? "analysis" : "raw");
  const [mode, setMode] = useState<VideoSourceMode>(src ? "raw" : initialMode);
  const resolvedPovSrc = povSrc ?? `${API_BASE}/video_feed/pov`;
  const resolvedAnnotatedSrc =
    annotatedSrc ?? `${API_BASE}/video_feed/annotated`;
  const resolvedRobotSrc = robotSrc ?? `${API_BASE}/video_feed/robot`;
  const resolvedRtmpSrc = rtmpSrc ?? RTMP_HLS_URL;

  const isRtmp = !src && mode === "rtmp";
  const mjpegBase =
    src ??
    resolveMjpegSrc(mode === "rtmp" ? "raw" : mode, {
      pov: resolvedPovSrc,
      annotated: resolvedAnnotatedSrc,
      robot: resolvedRobotSrc,
    });

  const imgRef = useRef<HTMLImageElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [rtmpWaiting, setRtmpWaiting] = useState(false);
  const [feedSrc, setFeedSrc] = useState(mjpegBase);

  const bumpSrc = useCallback(() => {
    const separator = mjpegBase.includes("?") ? "&" : "?";
    setFeedSrc(`${mjpegBase}${separator}t=${Date.now()}`);
    setReconnecting(true);
  }, [mjpegBase]);

  useEffect(() => {
    if (isRtmp) return;
    setFeedSrc(mjpegBase);
    setReconnecting(false);
  }, [mjpegBase, isRtmp]);

  useEffect(() => {
    if (!isRtmp) {
      hlsRef.current?.destroy();
      hlsRef.current = null;
      setRtmpWaiting(false);
      return;
    }

    const video = videoRef.current;
    if (!video) return;

    let cancelled = false;
    setRtmpWaiting(true);

    const markLive = () => {
      if (!cancelled) setRtmpWaiting(false);
    };
    const markWaiting = () => {
      if (!cancelled) setRtmpWaiting(true);
    };

    video.addEventListener("playing", markLive);
    video.addEventListener("waiting", markWaiting);
    video.addEventListener("stalled", markWaiting);
    video.addEventListener("error", markWaiting);

    if (Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: true,
        // Retry while publisher is offline so booth can wait for OBS/ffmpeg.
        manifestLoadingMaxRetry: Number.MAX_SAFE_INTEGER,
        manifestLoadingRetryDelay: 2000,
        levelLoadingMaxRetry: 6,
      });
      hlsRef.current = hls;
      hls.loadSource(resolvedRtmpSrc);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        void video.play().catch(() => undefined);
      });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (data.fatal) markWaiting();
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = resolvedRtmpSrc;
      void video.play().catch(() => undefined);
    } else {
      markWaiting();
    }

    return () => {
      cancelled = true;
      video.removeEventListener("playing", markLive);
      video.removeEventListener("waiting", markWaiting);
      video.removeEventListener("stalled", markWaiting);
      video.removeEventListener("error", markWaiting);
      hlsRef.current?.destroy();
      hlsRef.current = null;
      video.removeAttribute("src");
      video.load();
    };
  }, [isRtmp, resolvedRtmpSrc]);

  const handleLoad = () => {
    setReconnecting(false);
  };

  const handleError = () => {
    if (isRtmp) return;
    window.setTimeout(bumpSrc, 1500);
  };

  const sourceLabel =
    SOURCE_OPTIONS.find((option) => option.id === mode)?.label ?? mode;

  return (
    <section className={`nw-panel flex min-h-0 flex-col overflow-hidden ${className}`}>
      <div className="nw-panel-header bg-booth-panel">
        <div className="flex min-w-0 items-center gap-3">
          <span className="nw-kicker whitespace-nowrap">01 / Visual analysis</span>
          <span className="hidden h-3 w-px bg-booth-border sm:block" />
          <span className="hidden truncate text-[11px] font-medium text-booth-text sm:block">
            {label}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span
            className="nw-status"
            data-tone={reconnecting || rtmpWaiting ? "warn" : "live"}
          >
            {reconnecting || rtmpWaiting ? "Waiting" : "Live"}
          </span>
          {!src && (
            <div
              className="flex overflow-hidden border border-booth-border"
              role="group"
              aria-label="Video source"
            >
              {SOURCE_OPTIONS.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => setMode(option.id)}
                  className="nw-button min-h-0! rounded-none! border-0! px-2.5! py-1.5! text-[10px]! shadow-none!"
                  data-variant={mode === option.id ? "primary" : undefined}
                  aria-pressed={mode === option.id}
                >
                  {option.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="nw-rule-grid relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-[#111210]">
        {isRtmp ? (
          <video
            ref={videoRef}
            className="h-full w-full object-contain"
            muted
            autoPlay
            playsInline
            controls={false}
          />
        ) : (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            ref={imgRef}
            src={feedSrc}
            alt={label}
            className="h-full w-full object-contain"
            onLoad={handleLoad}
            onError={handleError}
          />
        )}
        <div className="pointer-events-none absolute inset-0 border-[10px] border-black/5" />
        <div className="pointer-events-none absolute left-3 top-3 font-data text-[9px] uppercase tracking-[0.14em] text-white/55">
          NW-CAM-01 · 960×540
        </div>
        <div className="pointer-events-none absolute bottom-3 right-3 bg-black/70 px-2 py-1 font-data text-[9px] uppercase tracking-[0.1em] text-white/75">
          Anonymous local inference
        </div>
        {(reconnecting || rtmpWaiting) && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-[#111210]/90">
            <div className="flex items-center gap-3 border border-white/20 bg-black/40 px-4 py-3 text-white">
              <span className="h-2 w-2 animate-live rounded-full bg-booth-warn" />
              <span className="font-data text-[10px] uppercase tracking-[0.12em]">
                {isRtmp
                  ? "Waiting for RTMP publisher"
                  : "Re-establishing camera stream"}
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="grid h-9 shrink-0 grid-cols-3 divide-x divide-booth-border border-t border-booth-border bg-booth-panel font-data text-[9px] uppercase tracking-[0.08em] text-booth-muted">
        <span className="flex items-center px-3">Source / {sourceLabel}</span>
        <span className="flex items-center px-3">Privacy / anonymous</span>
        <span className="flex items-center justify-end px-3 text-booth-success">
          Processing active
        </span>
      </div>
    </section>
  );
}
