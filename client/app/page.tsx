import Link from "next/link";

import { IntakeQueue } from "@/components/IntakeQueue";
import { LedgerPanel } from "@/components/LedgerPanel";
import { PlanStrip } from "@/components/PlanStrip";
import { ScoreCard } from "@/components/ScoreCard";
import { ThoughtTicker } from "@/components/ThoughtTicker";
import { VideoFeed } from "@/components/VideoFeed";

export default function BoothPage() {
  return (
    <main className="flex min-h-screen flex-col bg-booth-bg lg:h-screen lg:min-h-0">
      <header className="flex h-[68px] shrink-0 items-stretch border-b border-booth-border bg-booth-panel">
        <div className="flex w-[68px] shrink-0 items-center justify-center border-r border-booth-border bg-booth-ink text-white">
          <div className="font-data text-[10px] font-bold leading-[1.05] tracking-[0.16em]">
            NW
            <br />
            01
          </div>
        </div>
        <div className="flex min-w-0 flex-1 items-center justify-between px-3 sm:px-4 lg:px-5">
          <div className="hidden min-w-0 sm:block">
            <div className="flex items-baseline gap-3">
              <h1 className="truncate text-[20px] font-semibold tracking-[-0.025em] text-booth-text">
                Night Watch
              </h1>
              <span className="hidden text-[12px] text-booth-muted sm:inline">
                守夜犬 · Fatigue analysis console
              </span>
            </div>
            <p className="nw-kicker mt-1">
              Autonomous rest intervention / AdventureX 2026
            </p>
          </div>
          <nav className="ml-4 flex shrink-0 items-center gap-2">
            <span className="nw-status !hidden sm:!inline-flex" data-tone="live">
              System ready
            </span>
            <Link href="/form" className="nw-button hidden md:inline-flex">
              Intake
            </Link>
            <Link href="/lidar" className="nw-button" data-variant="primary">
              Spatial map ↗
            </Link>
          </nav>
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 lg:grid-cols-[minmax(0,1.62fr)_minmax(390px,1fr)]">
        <section className="grid min-h-[620px] grid-rows-[minmax(400px,1fr)_auto] gap-3 lg:min-h-0">
          <VideoFeed label="Camera 01 / fatigue overlay" className="min-h-0" />
          <PlanStrip />
        </section>

        <aside className="flex min-h-0 flex-col gap-3 overflow-y-auto lg:pr-1">
          <ScoreCard />
          <ThoughtTicker />
          <div className="grid shrink-0 gap-3 lg:grid-cols-2">
            <IntakeQueue />
            <LedgerPanel />
          </div>
        </aside>
      </div>
    </main>
  );
}
