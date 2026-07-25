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
  const [error, setError] = useState("");

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
    setError("");
    try {
      await acknowledgeIntake(responseId);
      setResponses((prev) => prev.filter((item) => item.response_id !== responseId));
    } catch {
      setError("机器人暂不可用，请稍后重试 | Robot unavailable");
    }
  };

  return (
    <section className="nw-panel overflow-hidden">
      <div className="nw-panel-header">
        <span className="nw-kicker">05 / Intake</span>
        <span className="font-data text-[9px] text-booth-muted">
          {responses.length.toString().padStart(2, "0")} pending
        </span>
      </div>
      {error && (
        <p className="border-b border-booth-border bg-[#fff2dc] px-3 py-2 font-data text-[9px] text-booth-warn">
          {error}
        </p>
      )}
      <div className="max-h-36 divide-y divide-booth-border overflow-y-auto bg-booth-panel-strong">
        {responses.length === 0 && (
          <p className="px-3 py-4 font-data text-[9px] uppercase tracking-[0.06em] text-booth-muted">
            No pending escorts
          </p>
        )}
        {responses.map((item) => (
          <div
            key={item.response_id}
            className="flex items-center justify-between gap-2 px-3 py-2.5"
          >
            <div className="min-w-0">
              <div className="truncate text-[11px] font-semibold text-booth-text">
                {item.name_alias || `访客 ${item.session_id.slice(0, 6)}`}
              </div>
              <div className="font-data mt-0.5 text-[8px] uppercase text-booth-muted">
                {item.tiredness === "tired" ? "Tired" : "Alert"} ·{" "}
                {formatTimeAgo(item.created_ts)}
              </div>
            </div>
            <button
              type="button"
              onClick={() => handleAcknowledge(item.response_id)}
              className="nw-button shrink-0"
              data-variant="primary"
            >
              Escort
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
