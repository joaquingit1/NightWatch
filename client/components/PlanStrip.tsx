"use client";

import { useEffect, useState } from "react";
import type { PlanResponse } from "@/lib/types";

const POLL_MS = 2000;

export function PlanStrip() {
  const [plan, setPlan] = useState<PlanResponse | null>(null);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const res = await fetch("/api/plan", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as PlanResponse;
        if (active) setPlan(data);
      } catch {
        // silent retry on next interval
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
      className="rounded-xl glass-panel p-4 animate-slide-up"
      style={{ animationDelay: '200ms' }}
    >
      <h2 className="mb-3 text-xs font-bold text-booth-muted/80">
        巡逻计划 Route Plan
      </h2>
      <div className="flex flex-wrap gap-2">
        {(plan?.stops ?? []).map((stop, i) => (
          <div
            key={`${stop.tag}-${i}`}
            className="rounded-lg border border-booth-border/50 bg-blue-50/50 px-3 py-2 text-sm transition-all hover:border-booth-accent/60 hover:bg-blue-100/50 backdrop-blur-sm"
          >
            <span className="font-semibold text-booth-text/90">{stop.tag}</span>
            <span className="ml-2 text-xs font-medium tracking-wide text-booth-accent/80">
              {stop.kind} · ETA <span className="text-booth-accent">{Math.round(stop.eta_s)}s</span>
            </span>
          </div>
        ))}
        {!plan?.stops?.length && (
          <span className="text-sm text-booth-muted">暂无计划 | No plan yet</span>
        )}
      </div>
    </section>
  );
}
