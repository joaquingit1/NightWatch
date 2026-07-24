"use client";

import { useCallback, useEffect, useRef, useState } from "react";

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
  povSrc = "/video_feed/pov",
  annotatedSrc = "/video_feed/annotated",
  label,
  className = "",
  defaultShowOverlay = true,
}: VideoFeedProps) {
  const [showOverlay, setShowOverlay] = useState(defaultShowOverlay);
  const baseSrc = src ?? (showOverlay ? annotatedSrc : povSrc);
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
    <div className={`relative overflow-hidden rounded-xl glass-panel ${className}`}>
      <div className="absolute right-3 top-3 z-10 flex items-center gap-2">
        {!src && (
          <button
            type="button"
            onClick={() => setShowOverlay((value) => !value)}
            className={`rounded-md px-2.5 py-1 text-[10px] font-bold border shadow-sm backdrop-blur-md transition-colors ${
              showOverlay
                ? "bg-booth-accent/10 text-booth-accent border-booth-accent/40"
                : "bg-white/90 text-booth-muted border-booth-border"
            }`}
            aria-pressed={showOverlay}
          >
            {showOverlay ? "Overlay on" : "Overlay off"}
          </button>
        )}
      </div>
      <div className="absolute left-3 top-3 z-10 rounded-md bg-white/90 px-2.5 py-1 text-[10px] font-bold text-booth-accent border border-booth-border shadow-sm backdrop-blur-md flex items-center gap-2">
        <span className="w-1.5 h-1.5 rounded-full bg-booth-danger animate-pulse"></span>
        {label}
      </div>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        ref={imgRef}
        src={feedSrc}
        alt={label}
        className="h-full w-full object-contain"
        onLoad={handleLoad}
        onError={handleError}
      />
      {reconnecting && (
        <div className="absolute inset-0 flex flex-col items-center justify-center bg-white/80 backdrop-blur-sm text-booth-warn z-20">
          <svg className="w-8 h-8 mb-3 text-booth-warn animate-spin-slow" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          <div className="text-sm font-medium animate-pulse">Reconnecting...</div>
        </div>
      )}
    </div>
  );
}
