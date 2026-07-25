"use client";

import { useEffect, useState } from "react";
import type { PlanResponse, RobotStatusResponse } from "@/lib/types";

const POLL_MS = 2000;

export function PlanStrip() {
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [robot, setRobot] = useState<RobotStatusResponse | null>(null);
  const [actionMessage, setActionMessage] = useState<string>("");
  const [actionBusy, setActionBusy] = useState(false);

  const sendRobotAction = async (
    action: "lie_down" | "stand_up" | "start_intake",
  ) => {
    setActionBusy(true);
    setActionMessage("");
    try {
      const response = await fetch("/api/robot/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const result = (await response.json()) as { ok: boolean; message: string };
      setActionMessage(result.message);
    } catch {
      setActionMessage("Robot command failed");
    } finally {
      setActionBusy(false);
    }
  };

  // Operator consent switch for public QR/NFC questionnaire responses
  // (AGENTS.md invariant 14): an unbound submission only reaches the robot
  // while a human holds this on. Server state, so it stays available while the
  // robot is offline, and the new value is applied optimistically until the
  // next status poll confirms it.
  const toggleAutoEscort = async () => {
    const enabled = !(robot?.auto_escort_enabled === true);
    setActionBusy(true);
    setActionMessage("");
    try {
      const response = await fetch("/api/robot/auto_escort", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const result = (await response.json()) as {
        ok: boolean;
        auto_escort_enabled: boolean;
        message: string;
      };
      setRobot((current) =>
        current
          ? { ...current, auto_escort_enabled: result.auto_escort_enabled }
          : current,
      );
      setActionMessage(result.message);
    } catch {
      setActionMessage("Auto-escort toggle failed");
    } finally {
      setActionBusy(false);
    }
  };

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const [planRes, robotRes] = await Promise.all([
          fetch("/api/plan", { cache: "no-store" }),
          fetch("/api/robot/status", { cache: "no-store" }),
        ]);
        if (active && planRes.ok) {
          setPlan((await planRes.json()) as PlanResponse);
        }
        if (active && robotRes.ok) {
          setRobot((await robotRes.json()) as RobotStatusResponse);
        }
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
    <section className="nw-panel overflow-hidden">
      <div className="nw-panel-header">
        <span className="nw-kicker">04 / Route &amp; posture control</span>
        <div className="flex items-center gap-2">
          {robot?.enabled && (
            <span
              className="nw-status"
              data-tone={robot.connected ? "live" : "warn"}
            >
              Robot {robot.connected ? "online" : "offline"}
            </span>
          )}
          {robot?.battery_soc != null && (
            <span className="font-data text-[9px] text-booth-muted">
              BAT {robot.battery_soc.toFixed(0)}%
            </span>
          )}
        </div>
      </div>

      {robot?.enabled && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-booth-border bg-booth-panel-strong px-3 py-2">
          <div className="flex items-center gap-3 font-data text-[9px] uppercase text-booth-muted">
            <span>
              Mode{" "}
              <strong className="font-semibold text-booth-text">
                {robot.behavior ?? "waiting"}
              </strong>
            </span>
            <span>
              Map{" "}
              <strong className="font-semibold text-booth-text">
                {robot.map_phase ?? "unknown"}
              </strong>
            </span>
            {robot.policy_active && (
              <span className="text-booth-accent">Care sequence active</span>
            )}
            {robot.hold_reason && (
              <span className="text-booth-warn">Hold / {robot.hold_reason}</span>
            )}
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              disabled={actionBusy}
              aria-pressed={robot.auto_escort_enabled === true}
              onClick={toggleAutoEscort}
              className="nw-button"
              data-variant={
                robot.auto_escort_enabled === true ? "primary" : undefined
              }
            >
              AUTO ESCORT 自动护送 /{" "}
              {robot.auto_escort_enabled === true ? "ON" : "OFF"}
            </button>
            <button
              type="button"
              disabled={!robot.connected || actionBusy}
              onClick={() => sendRobotAction("start_intake")}
              className="nw-button"
            >
              SHOW QR 邀请
            </button>
            <button
              type="button"
              disabled={!robot.connected || actionBusy}
              onClick={() => sendRobotAction("lie_down")}
              className="nw-button"
            >
              Lie down / hold
            </button>
            <button
              type="button"
              disabled={!robot.connected || actionBusy}
              onClick={() => sendRobotAction("stand_up")}
              className="nw-button"
              data-variant="primary"
            >
              Stand / resume
            </button>
          </div>
        </div>
      )}

      <div className="flex min-h-[76px] items-stretch overflow-x-auto bg-booth-panel">
        {(plan?.stops ?? []).map((stop, i) => (
          <div
            key={`${stop.tag}-${i}`}
            className="relative flex min-w-[156px] flex-1 flex-col justify-center border-r border-booth-border px-4 py-3 last:border-r-0"
          >
            <div className="mb-1 flex items-center gap-2">
              <span
                className={`h-2 w-2 ${
                  stop.kind === "home" ? "bg-booth-ink" : "bg-booth-accent"
                }`}
              />
              <span className="nw-kicker !text-[8px]">{stop.kind}</span>
            </div>
            <span className="truncate text-[12px] font-semibold text-booth-text">
              {stop.tag.replaceAll("_", " ")}
            </span>
            <span className="font-data mt-1 text-[9px] text-booth-muted">
              ETA {Math.round(stop.eta_s).toString().padStart(3, "0")} SEC
            </span>
          </div>
        ))}
        {!plan?.stops?.length && (
          <span className="flex items-center px-4 font-data text-[9px] uppercase tracking-[0.08em] text-booth-muted">
            Route calculation pending
          </span>
        )}
      </div>

      {actionMessage && (
        <div className="border-t border-booth-border px-3 py-2 font-data text-[9px] text-booth-muted">
          CONTROL RESPONSE / {actionMessage}
        </div>
      )}
    </section>
  );
}
