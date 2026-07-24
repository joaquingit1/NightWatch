"use client";

import { useEffect, useRef, useState } from "react";
import type { LedgerResponse, PolicyEvent } from "@/lib/types";

const MAX_EVENTS = 40;
const POLL_MS = 2000;
const BACKOFF_INITIAL_MS = 1000;
const BACKOFF_MAX_MS = 8000;

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function ledgerEventToPolicy(event: LedgerResponse["events"][number]): PolicyEvent {
  return {
    ts: event.ts,
    state: event.state,
    target_person: event.person_id,
    utterance: null,
    detail: event.detail,
  };
}

function parseDetail(detail: string): { zh: string; en: string } {
  const parts = detail.split("|").map((s) => s.trim());
  if (parts.length >= 2) return { zh: parts[0], en: parts[1] };
  return { zh: detail, en: "" };
}

function formatState(state: string): string {
  return state
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

function getStateColor(state: string) {
  switch (state) {
    case "WAKE_LADDER":
    case "CELEBRATE":
    case "NAP_REGISTERED":
      return { border: "border-emerald-500/50 hover:border-emerald-500", text: "text-emerald-600", bgHover: "hover:bg-emerald-50" };
    case "TRIAGE":
    case "DIAGNOSE":
    case "PRESCRIBE":
      return { border: "border-blue-500/50 hover:border-blue-500", text: "text-blue-600", bgHover: "hover:bg-blue-50" };
    case "ESCORT":
    case "PATROL":
    case "APPROACH":
      return { border: "border-purple-500/50 hover:border-purple-500", text: "text-purple-600", bgHover: "hover:bg-purple-50" };
    case "ESTOP":
    case "DETER":
      return { border: "border-amber-500/50 hover:border-amber-500", text: "text-amber-600", bgHover: "hover:bg-amber-50" };
    default:
      return { border: "border-slate-400/50 hover:border-slate-400", text: "text-slate-500", bgHover: "hover:bg-slate-50" };
  }
}

export function ThoughtTicker() {
  const [events, setEvents] = useState<PolicyEvent[]>([]);
  const [reconnecting, setReconnecting] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const backoffRef = useRef(BACKOFF_INITIAL_MS);
  const seenTsRef = useRef<Set<number>>(new Set());

  const mergeEvents = (incoming: PolicyEvent[]) => {
    if (!incoming.length) return;
    setEvents((prev) => {
      const merged = [...prev];
      for (const event of incoming) {
        if (seenTsRef.current.has(event.ts)) continue;
        seenTsRef.current.add(event.ts);
        merged.push(event);
      }
      return merged.slice(-MAX_EVENTS);
    });
  };

  useEffect(() => {
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let pollTimer: number | null = null;
    let cancelled = false;

    const bootstrap = async () => {
      try {
        const res = await fetch("/api/ledger", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as LedgerResponse;
        mergeEvents(data.events.map(ledgerEventToPolicy));
      } catch {
        // polling fallback will retry
      }
    };

    const pollLedger = async () => {
      try {
        const res = await fetch("/api/ledger", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as LedgerResponse;
        mergeEvents(data.events.slice(-MAX_EVENTS).map(ledgerEventToPolicy));
      } catch {
        // silent retry
      }
    };

    const connect = () => {
      if (cancelled) return;
      // Next.js rewrites buffer SSE; connect directly to FastAPI instead.
      source = new EventSource(`${API_BASE}/text_stream/thoughts`);

      source.onopen = () => {
        backoffRef.current = BACKOFF_INITIAL_MS;
        setReconnecting(false);
      };

      source.onmessage = (msg) => {
        try {
          const event = JSON.parse(msg.data) as PolicyEvent;
          mergeEvents([event]);
        } catch {
          // ignore malformed events
        }
      };

      source.onerror = () => {
        source?.close();
        setReconnecting(true);
        const delay = backoffRef.current;
        backoffRef.current = Math.min(BACKOFF_MAX_MS, delay * 2);
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    bootstrap();
    connect();
    pollTimer = window.setInterval(pollLedger, POLL_MS);

    return () => {
      cancelled = true;
      source?.close();
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (pollTimer) window.clearInterval(pollTimer);
    };
  }, []);

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [events]);

  return (
    <section
      className="flex min-h-0 flex-1 flex-col rounded-xl glass-panel p-4 animate-slide-up"
      style={{ animationDelay: "100ms" }}
    >
      <h2 className="mb-1 text-xs font-bold text-booth-muted/80 flex items-center justify-between">
        <span>思考流 Thought Ticker</span>
        {reconnecting && (
          <span className="text-[10px] text-booth-warn animate-pulse">Reconnecting...</span>
        )}
      </h2>
      <p className="mb-2 text-[10px] text-booth-muted leading-snug">
        守夜犬当前在做什么 · Live care-loop status (patrol, triage, escort…)
      </p>
      <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto pr-2 text-sm mt-1">
        {events.length === 0 && !reconnecting && (
          <p className="text-booth-muted">等待系统事件... Waiting for events...</p>
        )}
        {events.map((event, i) => {
          const { zh, en } = parseDetail(event.detail);
          const color = getStateColor(event.state);
          return (
            <div
              key={`${event.ts}-${i}`}
              className={`border-l-2 pl-3 animate-slide-up transition-all py-1 -ml-1 rounded-r-md ${color.border} ${color.bgHover}`}
            >
              <div className={`text-[10px] font-bold mb-0.5 ${color.text}`}>
                {formatState(event.state)}
              </div>
              <div className="text-[15px] font-medium leading-snug text-booth-text/90">{zh}</div>
              {en && <div className="text-xs text-booth-muted mt-0.5 font-light">{en}</div>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
