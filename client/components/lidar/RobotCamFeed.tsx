"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const RETRY_MS = 2500;

/** Picture-in-picture MJPEG feed of the robot's first-person camera,
 * proxied by the booth server at /video_feed/robot. Browsers do not
 * reliably fire load events for multipart/x-mixed-replace images (see
 * VideoFeed.tsx), so assume signal until an error proves otherwise and
 * reconnect on error with a cache-busting query param. */
export function RobotCamFeed({
  className = "",
  title = "ROBOT CAM",
  noSignalLabel = "NO SIGNAL · RETRYING",
  alt = "Robot first-person camera",
}: {
  className?: string;
  title?: string;
  noSignalLabel?: string;
  alt?: string;
}) {
  const [src, setSrc] = useState(`${API_BASE}/video_feed/robot`);
  const [hasSignal, setHasSignal] = useState(true);
  const retryTimer = useRef<number | undefined>(undefined);

  const retry = useCallback(() => {
    setHasSignal(true);
    setSrc(`${API_BASE}/video_feed/robot?t=${Date.now()}`);
  }, []);

  useEffect(() => {
    return () => window.clearTimeout(retryTimer.current);
  }, []);

  const handleError = () => {
    setHasSignal(false);
    window.clearTimeout(retryTimer.current);
    retryTimer.current = window.setTimeout(retry, RETRY_MS);
  };

  return (
    <div
      className={`pointer-events-auto overflow-hidden rounded-lg border border-cyan-500/25 bg-black/60 shadow-[0_0_24px_rgba(34,211,238,0.12)] backdrop-blur transition-all ${className}`}
    >
      <div className="flex items-center justify-between border-b border-cyan-500/15 px-2.5 py-1">
        <span className="text-[10px] tracking-widest text-cyan-300/70">
          {title}
        </span>
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            hasSignal ? "animate-pulse bg-emerald-400" : "bg-red-400/70"
          }`}
        />
      </div>
      <div className="relative aspect-video">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={alt}
          className="h-full w-full object-cover"
          onLoad={() => setHasSignal(true)}
          onError={handleError}
        />
        {!hasSignal && (
          <div className="absolute inset-0 flex items-center justify-center bg-[#04060c]/90">
            <span className="text-[10px] tracking-widest text-cyan-300/40">
              {noSignalLabel}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
