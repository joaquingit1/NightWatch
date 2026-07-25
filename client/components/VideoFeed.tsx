"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

interface VideoFeedProps {
  src?: string;
  povSrc?: string;
  annotatedSrc?: string;
  label: string;
  className?: string;
  defaultShowOverlay?: boolean;
}

export function VideoFeed({
  src,
  povSrc,
  annotatedSrc,
  label,
  className = "",
  defaultShowOverlay = true,
}: VideoFeedProps) {
  const [showOverlay, setShowOverlay] = useState(defaultShowOverlay);
  const resolvedPovSrc = povSrc ?? `${API_BASE}/video_feed/pov`;
  const resolvedAnnotatedSrc =
    annotatedSrc ?? `${API_BASE}/video_feed/annotated`;
  const baseSrc = src ?? (showOverlay ? resolvedAnnotatedSrc : resolvedPovSrc);
  const imgRef = useRef<HTMLImageElement>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [feedSrc, setFeedSrc] = useState(baseSrc);

  // multipart/x-mixed-replace MJPEG streams only fire a single `load` event
  // for the initial connection, not per-frame -- so we can't use `onLoad` as
  // a per-frame heartbeat to detect staleness. Only reconnect on a genuine
  // `onError` (the browser tore down the connection), otherwise leave the
  // single long-lived stream alone. Reconnecting on a timer here was
  // tearing down and restarting a perfectly healthy stream every few
  // seconds, which is what caused the visible flicker.
  const bumpSrc = useCallback(() => {
    const separator = baseSrc.includes("?") ? "&" : "?";
    setFeedSrc(`${baseSrc}${separator}t=${Date.now()}`);
    setReconnecting(true);
  }, [baseSrc]);

  useEffect(() => {
    setFeedSrc(baseSrc);
    setReconnecting(false);
  }, [baseSrc]);

  const handleLoad = () => {
    setReconnecting(false);
  };

  const handleError = () => {
    window.setTimeout(bumpSrc, 1500);
  };

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
            data-tone={reconnecting ? "warn" : "live"}
          >
            {reconnecting ? "Reconnecting" : "Live"}
          </span>
          {!src && (
            <button
              type="button"
              onClick={() => setShowOverlay((value) => !value)}
              className="nw-button"
              data-variant={showOverlay ? "primary" : undefined}
              aria-pressed={showOverlay}
            >
              Analysis {showOverlay ? "on" : "off"}
            </button>
          )}
        </div>
      </div>

      <div className="nw-rule-grid relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-[#111210]">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          ref={imgRef}
          src={feedSrc}
          alt={label}
          className="h-full w-full object-contain"
          onLoad={handleLoad}
          onError={handleError}
        />
        <div className="pointer-events-none absolute inset-0 border-[10px] border-black/5" />
        <div className="pointer-events-none absolute left-3 top-3 font-data text-[9px] uppercase tracking-[0.14em] text-white/55">
          NW-CAM-01 · 960×540
        </div>
        <div className="pointer-events-none absolute bottom-3 right-3 bg-black/70 px-2 py-1 font-data text-[9px] uppercase tracking-[0.1em] text-white/75">
          Anonymous local inference
        </div>
        {reconnecting && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-[#111210]/90">
            <div className="flex items-center gap-3 border border-white/20 bg-black/40 px-4 py-3 text-white">
              <span className="h-2 w-2 animate-live rounded-full bg-booth-warn" />
              <span className="font-data text-[10px] uppercase tracking-[0.12em]">
                Re-establishing camera stream
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="grid h-9 shrink-0 grid-cols-3 divide-x divide-booth-border border-t border-booth-border bg-booth-panel font-data text-[9px] uppercase tracking-[0.08em] text-booth-muted">
        <span className="flex items-center px-3">Source / local</span>
        <span className="flex items-center px-3">Privacy / anonymous</span>
        <span className="flex items-center justify-end px-3 text-booth-success">
          Processing active
        </span>
      </div>
    </section>
  );
}
