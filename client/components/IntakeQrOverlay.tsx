"use client";

import { useEffect, useState } from "react";

const POLL_MS = 2000;

interface SchemaStatus {
  interaction_active?: boolean;
  expires_at?: number | null;
}

/**
 * Full-screen QR invite shown while the robot holds an intake session open.
 * Visitors scan it with their phone to reach the consent form; the card
 * disappears as soon as the session resolves or expires.
 */
export function IntakeQrOverlay() {
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        // No `s=` override here: a sentinel session id never matches the
        // robot's active session, so the schema would always report
        // inactive. `source` only labels this poller in server logs.
        const res = await fetch("/api/form/schema?source=qr-overlay", {
          cache: "no-store",
        });
        if (!res.ok) throw new Error("schema unavailable");
        const schema = (await res.json()) as SchemaStatus;
        if (!active) return;
        setExpiresAt(
          schema.interaction_active && typeof schema.expires_at === "number"
            ? schema.expires_at
            : null,
        );
      } catch {
        if (active) setExpiresAt(null);
      }
    };

    poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(tick);
  }, []);

  const remaining = expiresAt === null ? 0 : Math.max(0, Math.ceil(expiresAt - now));
  if (expiresAt === null || remaining <= 0) return null;

  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-booth-ink/45 p-4 backdrop-blur-[2px]">
      <section className="nw-panel w-full max-w-sm bg-booth-panel-strong shadow-[0_18px_60px_rgba(17,17,16,0.35)]">
        <div className="nw-panel-header">
          <span className="nw-kicker">Intake / QR invite</span>
          <span className="nw-status" data-tone="live">
            Session open
          </span>
        </div>
        <div className="flex flex-col items-center gap-4 px-6 py-6 text-center">
          <h2 className="text-[18px] font-semibold leading-snug text-booth-text">
            扫码，我带你去休息 / Scan to get escorted
          </h2>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/api/intake/qr.png"
            alt="Intake form QR code"
            className="h-56 w-56 border border-booth-border bg-white p-2"
          />
          <div className="font-data text-[11px] uppercase tracking-[0.08em] text-booth-muted">
            Session closes in{" "}
            <span className="font-semibold text-booth-accent">
              {minutes}:{seconds.toString().padStart(2, "0")}
            </span>
          </div>
        </div>
      </section>
    </div>
  );
}
