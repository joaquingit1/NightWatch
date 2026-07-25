"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const RETRY_MS = 1500;

type FeedMode = "raw" | "analysis";

function feedUrl(mode: FeedMode, cacheBust = false) {
  const path =
    mode === "analysis" ? "/video_feed/annotated" : "/video_feed/robot";
  return `${API_BASE}${path}${cacheBust ? `?t=${Date.now()}` : ""}`;
}

export function FirstPersonViewer() {
  const [mode, setMode] = useState<FeedMode>("raw");
  const [src, setSrc] = useState(() => feedUrl("raw"));
  const [connected, setConnected] = useState(true);
  const [operatorUrl, setOperatorUrl] = useState(
    "http://localhost:5555/operator"
  );

  const selectMode = useCallback((nextMode: FeedMode) => {
    setMode(nextMode);
    setConnected(true);
    setSrc(feedUrl(nextMode, true));
  }, []);

  useEffect(() => {
    setOperatorUrl(`http://${window.location.hostname}:5555/operator`);
  }, []);

  useEffect(() => {
    if (connected) return;
    const retry = window.setTimeout(() => {
      setConnected(true);
      setSrc(feedUrl(mode, true));
    }, RETRY_MS);
    return () => window.clearTimeout(retry);
  }, [connected, mode]);

  return (
    <main className="fixed inset-0 overflow-hidden bg-[#04060c] text-white">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={
          mode === "analysis"
            ? "Robot first-person camera with analysis"
            : "Robot first-person camera"
        }
        className="h-full w-full object-contain"
        onLoad={() => setConnected(true)}
        onError={() => setConnected(false)}
      />

      <header className="absolute inset-x-0 top-0 flex flex-wrap items-center justify-between gap-3 border-b border-white/10 bg-black/65 px-4 py-3 font-mono backdrop-blur">
        <div>
          <p className="text-[9px] tracking-[0.2em] text-cyan-300/55">
            NIGHT WATCH / ROBOT CAMERA
          </p>
          <h1 className="mt-1 text-sm font-semibold tracking-[0.1em]">
            FIRST PERSON VIEW
          </h1>
        </div>
        <nav className="flex flex-wrap items-center gap-2" aria-label="View navigation">
          <button
            type="button"
            onClick={() => selectMode("raw")}
            aria-pressed={mode === "raw"}
            className={`border px-3 py-1.5 text-[10px] tracking-wider ${
              mode === "raw"
                ? "border-cyan-300 bg-cyan-400/20 text-cyan-100"
                : "border-white/20 bg-black/40 text-white/65"
            }`}
          >
            RAW
          </button>
          <button
            type="button"
            onClick={() => selectMode("analysis")}
            aria-pressed={mode === "analysis"}
            className={`border px-3 py-1.5 text-[10px] tracking-wider ${
              mode === "analysis"
                ? "border-amber-300 bg-amber-400/20 text-amber-100"
                : "border-white/20 bg-black/40 text-white/65"
            }`}
          >
            ANALYSIS
          </button>
          <Link
            href="/lidar"
            className="border border-white/20 bg-black/40 px-3 py-1.5 text-[10px] tracking-wider text-white/70"
          >
            LIDAR MAP
          </Link>
          <a
            href={operatorUrl}
            className="border border-white/20 bg-black/40 px-3 py-1.5 text-[10px] tracking-wider text-white/70"
          >
            OPERATOR CONSOLE
          </a>
        </nav>
      </header>

      <div className="absolute bottom-4 left-4 flex items-center gap-2 border border-white/15 bg-black/65 px-3 py-2 font-mono text-[10px] tracking-wider backdrop-blur">
        <span
          className={`h-2 w-2 rounded-full ${
            connected ? "animate-pulse bg-emerald-400" : "bg-red-400"
          }`}
        />
        {connected
          ? `${mode === "analysis" ? "ANALYSIS" : "RAW"} FEED`
          : "NO SIGNAL · RETRYING"}
      </div>
    </main>
  );
}
