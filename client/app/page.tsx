import "./landing.css";

import { Anton, IBM_Plex_Mono, Plus_Jakarta_Sans } from "next/font/google";

import { LenisProvider } from "@/components/LenisProvider";
import { CtaFooter } from "@/components/landing/CtaFooter";
import { Hero } from "@/components/landing/Hero";
import { LandingLanguageProvider } from "@/components/landing/LandingLanguage";
import { PinnedVisual } from "@/components/landing/PinnedVisual";
import { SignalsSection } from "@/components/landing/SignalsSection";
import { StorySection } from "@/components/landing/StorySection";
import { TechStripSection } from "@/components/landing/TechStripSection";
import { TimelineSection } from "@/components/landing/TimelineSection";

const anton = Anton({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-display",
});
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-mono",
});
const plusJakartaSans = Plus_Jakarta_Sans({
  subsets: ["latin"],
  weight: ["600", "700", "800"],
  variable: "--font-heading",
});

export default function LandingPage() {
  return (
    <LenisProvider>
      <LandingLanguageProvider>
        <div
          className={`landing-page ${anton.variable} ${plexMono.variable} ${plusJakartaSans.variable}`}
        >
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
      </LandingLanguageProvider>
    </LenisProvider>
  );
}
