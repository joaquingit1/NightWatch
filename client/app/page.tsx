import { IntakeQueue } from "@/components/IntakeQueue";
import { LedgerPanel } from "@/components/LedgerPanel";
import { PlanStrip } from "@/components/PlanStrip";
import { ScoreCard } from "@/components/ScoreCard";
import { ThoughtTicker } from "@/components/ThoughtTicker";
import { VideoFeed } from "@/components/VideoFeed";

export default function BoothPage() {
  return (
    <main className="flex h-screen flex-col bg-booth-bg p-4">
      <header className="mb-4 flex shrink-0 items-end justify-between border-b border-booth-border pb-4">
        <div>
          <h1 className="text-4xl font-extrabold tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-booth-text to-booth-accent">
            守夜犬 <span className="text-booth-accent font-light">Night Watch</span>
          </h1>
          <p className="mt-1 text-lg text-booth-muted">
            每个 AI 都想让你更努力。它想让你休息。
          </p>
          <p className="text-sm text-booth-muted">
            Every AI makes you work more. This one makes you stop.
          </p>
        </div>
        <div className="glass-panel rounded-lg px-4 py-2 text-right text-sm text-booth-muted flex flex-col justify-center">
          <div className="font-medium tracking-wide">AdventureX 2026</div>
          <div className="text-booth-accent font-semibold text-xs mt-0.5">#AdventureX2026 · Reverse</div>
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[3fr_2fr]">
        <VideoFeed
          label="Live Feed"
          className="min-h-[320px] lg:min-h-0"
        />

        <div className="flex min-h-0 flex-col gap-4">
          <ScoreCard />
          <ThoughtTicker />
          <PlanStrip />
          <IntakeQueue />
          <LedgerPanel />
        </div>
      </div>
    </main>
  );
}
