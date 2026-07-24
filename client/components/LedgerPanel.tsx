"use client";

import { useEffect, useState } from "react";
import type { LedgerResponse } from "@/lib/types";

const POLL_MS = 3000;

function formatState(state: string): string {
  return state
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

function getStateTextColor(state: string) {
  switch (state) {
    case "WAKE_LADDER":
    case "CELEBRATE":
    case "NAP_REGISTERED":
      return "text-emerald-600";
    case "TRIAGE":
    case "DIAGNOSE":
    case "PRESCRIBE":
      return "text-blue-600";
    case "ESCORT":
    case "PATROL":
    case "APPROACH":
      return "text-purple-600";
    case "ESTOP":
    case "DETER":
      return "text-amber-600";
    default:
      return "text-slate-500";
  }
}

export function LedgerPanel() {
  const [ledger, setLedger] = useState<LedgerResponse | null>(null);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const res = await fetch("/api/ledger", { cache: "no-store" });
        if (res.ok && active) {
          setLedger((await res.json()) as LedgerResponse);
        }
      } catch {
        // silent retry
      }
    };

    poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <section
      className="rounded-xl glass-panel p-4 animate-slide-up overflow-hidden relative"
      style={{ animationDelay: "300ms" }}
    >
      <h2 className="mb-3 text-xs font-bold text-booth-muted/80">账本 Ledger</h2>
      <div className="space-y-3 animate-slide-up">
        <div className="flex gap-4 text-xs font-semibold text-booth-text/70 bg-slate-50 rounded-md p-2 border border-booth-border/50">
          <span>Naps: {ledger?.nap_count ?? 0}</span>
          <span>Passes: {ledger?.pass_count ?? 0}</span>
        </div>
        <div className="max-h-32 space-y-1 overflow-y-auto text-xs">
          {(ledger?.events ?? []).slice(-8).reverse().map((ev, i) => (
            <div key={`${ev.ts}-${i}`} className="text-booth-muted">
              <span className={`font-bold ${getStateTextColor(ev.state)}`}>
                {formatState(ev.state)}
              </span>{" "}
              <span className="ml-1">{ev.detail}</span>
            </div>
          ))}
          {!ledger?.events?.length && (
            <p className="text-sm text-booth-muted px-2 italic">
              暂无记录 | No events yet
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
