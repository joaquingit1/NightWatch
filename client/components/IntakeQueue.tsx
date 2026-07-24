"use client";

import { useCallback, useEffect, useState } from "react";
import {
  acknowledgeIntake,
  fetchPendingEscort,
  type IntakeResponse,
} from "@/lib/intake";

const POLL_MS = 3000;

function formatTimeAgo(ts: number): string {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

export function IntakeQueue() {
  const [responses, setResponses] = useState<IntakeResponse[]>([]);

  const poll = useCallback(async () => {
    const pending = await fetchPendingEscort();
    setResponses(pending);
  }, []);

  useEffect(() => {
    poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => window.clearInterval(timer);
  }, [poll]);

  const handleAcknowledge = async (responseId: string) => {
    await acknowledgeIntake(responseId);
    setResponses((prev) => prev.filter((item) => item.response_id !== responseId));
  };

  return (
    <section
      className="rounded-xl glass-panel p-4 animate-slide-up"
      style={{ animationDelay: "350ms" }}
    >
      <h2 className="mb-3 text-xs font-bold text-booth-muted/80">
        休息登记队列 Intake queue
      </h2>
      <div className="space-y-2 max-h-36 overflow-y-auto">
        {responses.length === 0 && (
          <p className="text-sm text-booth-muted italic px-1">暂无待引导访客 | No pending escorts</p>
        )}
        {responses.map((item) => (
          <div
            key={item.response_id}
            className="flex items-center justify-between gap-2 rounded-lg border border-booth-accent/20 bg-blue-50/50 px-3 py-2 text-sm"
          >
            <div className="min-w-0">
              <div className="font-semibold text-booth-text truncate">
                {item.name_alias || `访客 ${item.session_id.slice(0, 6)}`}
              </div>
              <div className="text-xs text-booth-muted">
                {item.tiredness === "tired" ? "有点累" : "还精神"} · {formatTimeAgo(item.created_ts)}
              </div>
            </div>
            <button
              type="button"
              onClick={() => handleAcknowledge(item.response_id)}
              className="shrink-0 rounded-md bg-booth-accent px-2.5 py-1 text-[10px] font-bold text-white hover:bg-blue-600"
            >
              Ack
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
