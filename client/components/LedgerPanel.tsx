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
      return "text-booth-success";
    case "TRIAGE":
    case "DIAGNOSE":
    case "PRESCRIBE":
      return "text-booth-accent";
    case "ESCORT":
    case "PATROL":
    case "APPROACH":
      return "text-booth-text";
    case "ESTOP":
    case "DETER":
      return "text-booth-warn";
    default:
      return "text-booth-muted";
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
    <section className="nw-panel overflow-hidden">
      <div className="nw-panel-header">
        <span className="nw-kicker">06 / Care ledger</span>
        <span className="font-data text-[9px] text-booth-muted">
          local session
        </span>
      </div>
      <div>
        <div className="grid grid-cols-2 divide-x divide-booth-border border-b border-booth-border bg-booth-panel-strong">
          <div className="px-3 py-2.5">
            <div className="nw-kicker !text-[8px]">Naps</div>
            <div className="font-data mt-1 text-xl font-semibold">
              {(ledger?.nap_count ?? 0).toString().padStart(2, "0")}
            </div>
          </div>
          <div className="px-3 py-2.5">
            <div className="nw-kicker !text-[8px]">Passes</div>
            <div className="font-data mt-1 text-xl font-semibold">
              {(ledger?.pass_count ?? 0).toString().padStart(2, "0")}
            </div>
          </div>
        </div>
        {(ledger?.active_naps ?? []).map((nap) => (
          <div
            key={nap.nap_id}
            className="flex items-center justify-between border-b border-booth-border bg-[#e7f4ef] px-3 py-2 font-data text-[9px]"
          >
            <span className="font-semibold text-booth-success">
              {nap.name_alias.toUpperCase()} / RESTING
            </span>
            <span className="text-booth-success">
              CHECK {Math.ceil(nap.remaining_s / 60)}M
            </span>
          </div>
        ))}
        <div className="max-h-12 divide-y divide-booth-border overflow-y-auto bg-booth-panel-strong">
          {(ledger?.events ?? []).slice(-8).reverse().map((ev, i) => (
            <div
              key={`${ev.ts}-${i}`}
              className="truncate px-3 py-1.5 font-data text-[8px] text-booth-muted"
            >
              <span className={`font-bold ${getStateTextColor(ev.state)}`}>
                {formatState(ev.state)}
              </span>{" "}
              <span className="ml-1">{ev.detail.split("|")[0]}</span>
            </div>
          ))}
          {!ledger?.events?.length && (
            <p className="px-3 py-4 font-data text-[9px] uppercase tracking-[0.06em] text-booth-muted">
              No care events recorded
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
