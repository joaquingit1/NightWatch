"use client";

import { useEffect, useState } from "react";
import type { FatigueFrame } from "@/lib/types";

const POLL_MS = 500;

function clampPercent(value: number, max: number): number {
  return Math.min(100, Math.max(0, (value / max) * 100));
}

function scoreTone(score: number): {
  color: string;
  label: string;
  status: "live" | "warn" | "danger";
} {
  if (score >= 60) {
    return { color: "bg-booth-danger", label: "Rest advised", status: "danger" };
  }
  if (score >= 40) {
    return { color: "bg-booth-warn", label: "Observe", status: "warn" };
  }
  return { color: "bg-booth-accent", label: "Nominal", status: "live" };
}

function signalCell(label: string, value: number, max: number, unit = "") {
  return (
    <div key={label} className="border-t border-booth-border px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="nw-kicker !text-[8px] !tracking-[0.08em]">{label}</span>
        <span className="font-data text-[11px] font-semibold text-booth-text">
          {value.toFixed(1)}
          {unit}
        </span>
      </div>
      <div className="mt-2 h-[2px] bg-[#d9d6ce]">
        <div
          className="h-full bg-booth-accent transition-[width] duration-300"
          style={{ width: `${clampPercent(value, max)}%` }}
        />
      </div>
    </div>
  );
}

function trackId(personId: string | null): string {
  return (personId ?? "track-?").replace("track-", "ID–");
}

export function ScoreCard() {
  const [frame, setFrame] = useState<FatigueFrame | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const res = await fetch("/api/score", { cache: "no-store" });
        if (!res.ok) throw new Error("score fetch failed");
        const data = (await res.json()) as FatigueFrame;
        if (active) {
          setFrame(data);
          setError(false);
        }
      } catch {
        if (active) setError(true);
      }
    };

    poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const score = frame?.score ?? 0;
  const confidence = frame?.confidence ?? 0;
  const quality = frame?.quality ?? 0;
  const tracks = frame?.people?.length
    ? frame.people
    : frame?.person_id
      ? [frame]
      : [];
  const modelLive = frame?.source_status === "live" && !error;
  const tone = scoreTone(score);

  return (
    <section className="nw-panel shrink-0 overflow-hidden">
      <div className="nw-panel-header">
        <span className="nw-kicker">02 / Rest index</span>
        <div className="flex items-center gap-2">
          <span className="font-data text-[9px] text-booth-muted">
            {frame ? `${frame.processing_ms.toFixed(0)} MS` : "— MS"}
          </span>
          <span
            className="nw-status"
            data-tone={modelLive ? "live" : "warn"}
          >
            {modelLive ? "Model live" : "Model offline"}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-[1fr_126px] border-b border-booth-border">
        <div className="px-4 py-2">
          <div className="flex items-end gap-3">
            <span className="font-data text-[70px] font-medium leading-[0.9] tracking-[-0.08em] text-booth-text">
              {Math.round(score).toString().padStart(2, "0")}
            </span>
            <div className="mb-1.5">
              <div className="font-data text-[10px] text-booth-muted">/ 100</div>
              <div className="mt-1 text-[11px] font-semibold text-booth-text">
                {tone.label}
              </div>
            </div>
          </div>

          <div className="mt-3">
            <div className="relative h-2 bg-[#d8d5cc]">
              <div
                className={`h-full ${tone.color} transition-[width] duration-300`}
                style={{ width: `${Math.min(100, Math.max(0, score))}%` }}
              />
              <span className="absolute left-[40%] top-0 h-2 w-px bg-booth-ink/50" />
              <span className="absolute left-[60%] top-0 h-2 w-px bg-booth-ink/50" />
            </div>
            <div className="font-data mt-1 flex justify-between text-[8px] text-booth-muted">
              <span>0 / ALERT</span>
              <span>40</span>
              <span>60</span>
              <span>100 / REST</span>
            </div>
          </div>
        </div>

        <div className="grid grid-rows-2 divide-y divide-booth-border border-l border-booth-border">
          <div className="flex flex-col justify-center px-3">
            <span className="nw-kicker !text-[8px]">Confidence</span>
            <span className="font-data mt-1 text-xl font-semibold">
              {(confidence * 100).toFixed(0)}
              <span className="text-[10px] text-booth-muted">%</span>
            </span>
          </div>
          <div className="flex flex-col justify-center px-3">
            <span className="nw-kicker !text-[8px]">Signal quality</span>
            <span className="font-data mt-1 text-xl font-semibold">
              {(quality * 100).toFixed(0)}
              <span className="text-[10px] text-booth-muted">%</span>
            </span>
          </div>
        </div>
      </div>

      <div className="border-b border-booth-border">
        <div className="flex items-center justify-between px-3 py-2">
          <span className="nw-kicker !text-[8px]">Anonymous subjects</span>
          <span className="font-data text-[9px] text-booth-muted">
            {tracks.length.toString().padStart(2, "0")} active ·{" "}
            {frame?.model_version ?? "waiting for model"}
          </span>
        </div>

        {tracks.length ? (
          <div className="divide-y divide-booth-border border-t border-booth-border">
            {tracks.map((track) => {
              const isPrimary = track.person_id === frame?.person_id;
              const calibrated = track.calib_state === "full";
              return (
                <div
                  key={track.person_id ?? `sequence-${track.sequence}`}
                  className={`grid grid-cols-[44px_1fr_52px_58px_74px] items-center px-3 py-1.5 font-data text-[9px] ${
                    isPrimary ? "bg-booth-accent-soft" : "bg-booth-panel"
                  }`}
                >
                  <span className="font-bold text-booth-text">
                    {trackId(track.person_id)}
                  </span>
                  <span className="truncate text-booth-muted">
                    {track.status}
                  </span>
                  <span className="text-right text-booth-text">
                    R {track.score.toFixed(0)}
                  </span>
                  <span className="text-right text-booth-text">
                    Q {(track.quality * 100).toFixed(0)}
                  </span>
                  <span className="text-right text-booth-muted">
                    {calibrated
                      ? "CALIBRATED"
                      : `CAL ${(track.calibration_progress * 100).toFixed(0)}%`}
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="border-t border-booth-border px-3 py-3 font-data text-[9px] uppercase tracking-[0.08em] text-booth-muted">
            No clear, consented subject in frame
          </div>
        )}
      </div>

      {frame && (
        <div className="grid grid-cols-2 bg-booth-panel-strong">
          {signalCell("PERCLOS", frame.factors.perclos, 1)}
          {signalCell("Blink p90", frame.factors.blink_ms_p90, 800, "ms")}
          {signalCell("Nods", frame.factors.nod_count, 5)}
          {signalCell("Yawns", frame.factors.yawn_count, 3)}
          {signalCell("Slump", frame.factors.slump_deg, 30, "°")}
          {signalCell("Movement", frame.factors.movement_entropy, 5)}
        </div>
      )}
    </section>
  );
}
