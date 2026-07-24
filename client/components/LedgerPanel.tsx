"use client";

import { useEffect, useState } from "react";
import type { LeaderboardResponse, LedgerResponse } from "@/lib/types";

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
  const [leaderboard, setLeaderboard] = useState<LeaderboardResponse | null>(null);
  const [showLeaderboard, setShowLeaderboard] = useState(true);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const [ledgerRes, boardRes] = await Promise.all([
          fetch("/api/ledger", { cache: "no-store" }),
          fetch("/api/leaderboard", { cache: "no-store" }),
        ]);
        if (ledgerRes.ok && active) {
          setLedger((await ledgerRes.json()) as LedgerResponse);
        }
        if (boardRes.ok && active) {
          setLeaderboard((await boardRes.json()) as LeaderboardResponse);
        }
      } catch {
        // silent retry
      }
    };

    poll();
    const timer = window.setInterval(poll, POLL_MS);
    const rotate = window.setInterval(() => setShowLeaderboard((v) => !v), 8000);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.clearInterval(rotate);
    };
  }, []);

  return (
    <section 
      className="rounded-xl glass-panel p-4 animate-slide-up overflow-hidden relative"
      style={{ animationDelay: '300ms' }}
    >
      <h2 className="mb-3 text-xs font-bold text-booth-muted/80">
        {showLeaderboard ? "排行榜 Leaderboard" : "账本 Ledger"}
      </h2>
      {showLeaderboard ? (
        <div className="space-y-2 animate-slide-up">
          {(leaderboard?.entries ?? []).map((entry, i) => (
            <div key={entry.name_alias} className={`flex items-center justify-between text-sm rounded-md px-2 py-1.5 transition-colors ${i < 3 ? 'bg-blue-50 border border-booth-accent/20 font-medium text-booth-text' : 'text-booth-muted hover:bg-slate-50 hover:text-booth-text/90'}`}>
              <span className="flex items-center gap-2">
                <span className={`w-5 text-center ${i < 3 ? 'text-booth-accent font-bold' : 'text-booth-muted/50'}`}>#{i + 1}</span>
                {entry.name_alias}
              </span>
              <span className={i < 3 ? 'text-booth-accent font-semibold' : 'text-booth-muted'}>
                {entry.peak_score} <span className="opacity-50 font-normal">· {entry.nap_count} naps</span>
              </span>
            </div>
          ))}
          {!leaderboard?.entries?.length && (
            <p className="text-sm text-booth-muted px-2 italic">暂无领养者 | No adopters yet</p>
          )}
        </div>
      ) : (
        <div className="space-y-3 animate-slide-up">
          <div className="flex gap-4 text-xs font-semibold text-booth-text/70 bg-slate-50 rounded-md p-2 border border-booth-border/50">
            <span>Naps: {ledger?.nap_count ?? 0}</span>
            <span>Passes: {ledger?.pass_count ?? 0}</span>
          </div>
          <div className="max-h-32 space-y-1 overflow-y-auto text-xs">
            {(ledger?.events ?? []).slice(-8).reverse().map((ev, i) => (
              <div key={`${ev.ts}-${i}`} className="text-booth-muted">
                <span className={`font-bold ${getStateTextColor(ev.state)}`}>{formatState(ev.state)}</span> <span className="ml-1">{ev.detail}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
