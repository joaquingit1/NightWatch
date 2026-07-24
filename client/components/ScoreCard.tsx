"use client";

import { useEffect, useState } from "react";
import type { FatigueFrame } from "@/lib/types";

const POLL_MS = 500;

function factorBar(label: string, value: number, max: number) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  return (
    <div key={label} className="space-y-1">
      <div className="flex justify-between text-xs font-medium text-booth-muted">
        <span>{label}</span>
        <span className="text-booth-text">{value.toFixed(1)}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full rounded-full bg-booth-accent transition-[width] duration-300 ease-linear"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1).toLowerCase();
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
  const lowConfidence = confidence < 0.5;

  return (
    <section className="rounded-xl glass-panel p-5 animate-slide-up">
      <h2 className="mb-3 text-xs font-bold text-booth-muted/80">
        RestScore 疲劳指数
      </h2>
      <div className="flex items-end gap-3">
        <span
          className={`text-7xl font-bold leading-none transition-colors duration-500 ${
            score >= 60 ? "text-booth-danger" : score >= 40 ? "text-booth-warn" : "text-booth-accent"
          }`}
        >
          {Math.round(score)}
        </span>
        <span className="mb-2 text-2xl font-light text-booth-muted/60">/ 100</span>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full transition-[width] duration-300 ease-linear ${
            score >= 60 ? "bg-booth-danger" : score >= 40 ? "bg-booth-warn" : "bg-booth-accent"
          }`}
          style={{ width: `${Math.min(100, Math.max(0, score))}%` }}
        />
      </div>
      <div className="mt-2 flex items-center justify-between text-sm text-booth-muted">
        <div>
          {lowConfidence
            ? "置信度不足，暂不判定 | Low confidence"
            : `置信度 ${(confidence * 100).toFixed(0)}%`}
        </div>
        {frame && (
          <div className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${
            frame.calib_state === 'full' ? 'bg-emerald-100 text-emerald-700 border border-emerald-200' :
            frame.calib_state === 'quick' ? 'bg-blue-100 text-blue-700 border border-blue-200' :
            'bg-slate-100 text-slate-500 border border-slate-200'
          }`}>
            Calib: {capitalize(frame.calib_state)}
          </div>
        )}
      </div>
      {error && (
        <p className="mt-2 text-sm text-booth-warn">重新连接中 reconnecting...</p>
      )}
      {frame && (
        <div className="mt-4 space-y-2">
          {factorBar("PERCLOS", frame.factors.perclos, 1)}
          {factorBar("Blink Duration (ms)", frame.factors.blink_ms_p90, 800)}
          {factorBar("Nods", frame.factors.nod_count, 5)}
          {factorBar("Yawns", frame.factors.yawn_count, 3)}
          {factorBar("Slump", frame.factors.slump_deg, 30)}
          {factorBar("Movement Entropy", frame.factors.movement_entropy, 5)}
          {factorBar("Sedentary Hours", frame.factors.sedentary_hours, 4)}
        </div>
      )}
    </section>
  );
}
