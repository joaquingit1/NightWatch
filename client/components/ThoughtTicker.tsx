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
  return state.replaceAll("_", " ");
}

function getStateTone(state: string): string {
  switch (state) {
    case "WAKE_LADDER":
    case "CELEBRATE":
    case "NAP_REGISTERED":
      return "bg-booth-success";
    case "TRIAGE":
    case "DIAGNOSE":
    case "PRESCRIBE":
      return "bg-booth-accent";
    case "ESCORT":
    case "PATROL":
    case "APPROACH":
      return "bg-booth-ink";
    case "ESTOP":
    case "DETER":
      return "bg-booth-danger";
    default:
      return "bg-booth-border-strong";
  }
}

function formatEventTime(ts: number): string {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(ts * 1000));
}

export function ThoughtTicker() {
  const [events, setEvents] = useState<PolicyEvent[]>([]);
  const [reconnecting, setReconnecting] = useState(false);
  const [audioEnabled, setAudioEnabled] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioEnabledRef = useRef(false);
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

  const setAudio = (enabled: boolean) => {
    setAudioEnabled(enabled);
    audioEnabledRef.current = enabled;
    window.localStorage.setItem("nightwatch-audio-enabled", enabled ? "1" : "0");
    if (!enabled && audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
  };

  const playCue = (cue: string | null) => {
    if (!audioEnabledRef.current || !cue) return;
    audioRef.current?.pause();
    const audio = new Audio(`/api/audio/${encodeURIComponent(cue)}`);
    audio.volume = 0.8;
    audioRef.current = audio;
    void audio.play().catch(() => {
      // The visible toggle remains the fallback if browser autoplay policy
      // changes or the booth audio device is unavailable.
    });
  };

  useEffect(() => {
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let pollTimer: number | null = null;
    let cancelled = false;
    let streamOpen = false;
    const savedAudio = window.localStorage.getItem("nightwatch-audio-enabled") === "1";
    setAudioEnabled(savedAudio);
    audioEnabledRef.current = savedAudio;

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
      // SSE already delivers each event immediately. Polling the same ledger
      // while that stream is healthy duplicated JSON work and React updates;
      // retain polling only as the connection-loss fallback.
      if (streamOpen) return;
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
        streamOpen = true;
        backoffRef.current = BACKOFF_INITIAL_MS;
        setReconnecting(false);
      };

      source.onmessage = (msg) => {
        try {
          const event = JSON.parse(msg.data) as PolicyEvent;
          mergeEvents([event]);
          playCue(event.utterance);
        } catch {
          // ignore malformed events
        }
      };

      source.onerror = () => {
        streamOpen = false;
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
      audioRef.current?.pause();
    };
  }, []);

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [events]);

  return (
    <section className="nw-panel flex h-[220px] min-h-[220px] shrink-0 flex-col overflow-hidden">
      <div className="nw-panel-header">
        <div className="flex items-center gap-3">
          <span className="nw-kicker">03 / Decision log</span>
          {reconnecting && (
            <span className="font-data text-[9px] uppercase text-booth-warn">
              Stream reconnecting
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden font-data text-[9px] text-booth-muted sm:inline">
            {events.length.toString().padStart(2, "0")} records
          </span>
          <button
            type="button"
            onClick={() => setAudio(!audioEnabled)}
            aria-pressed={audioEnabled}
            className="nw-button"
            data-variant={audioEnabled ? "primary" : undefined}
          >
            Audio {audioEnabled ? "on" : "off"}
          </button>
        </div>
      </div>

      <div
        ref={listRef}
        className="min-h-[178px] flex-1 divide-y divide-booth-border overflow-y-auto bg-booth-panel-strong"
      >
        {events.length === 0 && !reconnecting && (
          <p className="p-4 font-data text-[9px] uppercase tracking-[0.08em] text-booth-muted">
            Waiting for policy events
          </p>
        )}
        {events.map((event, i) => {
          const { zh, en } = parseDetail(event.detail);
          return (
            <div
              key={`${event.ts}-${i}`}
              className="grid grid-cols-[58px_84px_1fr] gap-2 px-3 py-2.5"
            >
              <time className="font-data pt-0.5 text-[8px] text-booth-muted">
                {formatEventTime(event.ts)}
              </time>
              <div className="flex items-start gap-2 pt-0.5 font-data text-[8px] font-bold uppercase tracking-[0.06em] text-booth-text">
                <span
                  className={`mt-[2px] h-1.5 w-1.5 shrink-0 ${getStateTone(event.state)}`}
                />
                {formatState(event.state)}
              </div>
              <div className="min-w-0">
                <div className="text-[12px] font-medium leading-[1.35] text-booth-text">
                  {zh}
                </div>
                {en && (
                  <div className="mt-0.5 text-[10px] leading-[1.35] text-booth-muted">
                    {en}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
      <div className="flex h-7 shrink-0 items-center justify-between border-t border-booth-border px-3 font-data text-[8px] uppercase tracking-[0.08em] text-booth-muted">
        <span>Deterministic care policy</span>
        <span>Newest event ↓</span>
      </div>
    </section>
  );
}
