"use client";

import { Scene3D } from "./Scene3D";

export function PinnedVisual() {
  return (
    <div className="landing-pinned-visual">
      <div className="landing-pinned-visual-inner">
        <Scene3D className="landing-pinned-canvas" />
        <div className="landing-hero-grain" aria-hidden="true" />
        <div className="landing-target landing-target--top" aria-hidden="true" />
        <div className="landing-target landing-target--bottom" aria-hidden="true" />
      </div>
    </div>
  );
}
