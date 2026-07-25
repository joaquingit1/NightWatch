import "./landing.css";

import { CtaFooter } from "@/components/landing/CtaFooter";
import { Hero } from "@/components/landing/Hero";
import { PinnedVisual } from "@/components/landing/PinnedVisual";
import { SignalsSection } from "@/components/landing/SignalsSection";
import { StorySection } from "@/components/landing/StorySection";
import { TechStripSection } from "@/components/landing/TechStripSection";
import { TimelineSection } from "@/components/landing/TimelineSection";

export default function LandingPage() {
  return (
    <div className="landing-page">
      <div id="landing-pinned" className="landing-pinned">
        <div className="landing-pinned-content">
          <Hero />
          <StorySection />
          <TimelineSection />
        </div>
        <PinnedVisual />
      </div>
      <SignalsSection />
      <TechStripSection />
      <CtaFooter />
    </div>
  );
}
