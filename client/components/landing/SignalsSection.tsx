"use client";

import { motion, useReducedMotion } from "framer-motion";

import { ParallaxHeading } from "./ParallaxHeading";

const SIGNALS = [
  {
    title: "Eye openness",
    body: "PERCLOS and blink duration, corrected for head angle so looking sideways does not fake drowsiness.",
  },
  {
    title: "Head nods & yawns",
    body: "Micro-movements that accumulate across a rolling observation window.",
  },
  {
    title: "Slump",
    body: "Neck-torso angle when body pose is visible — posture tells a story a single frame cannot.",
  },
  {
    title: "Stillness",
    body: "Movement patterns over time feed a RestScore from 0 to 100 with a confidence indicator.",
  },
] as const;

export function SignalsSection() {
  const reduceMotion = useReducedMotion();

  return (
    <section className="landing-section landing-signals">
      <div className="landing-section-inner">
        <ParallaxHeading kicker="HOW FATIGUE IS DETECTED">
          Not one blink.
          <br />
          A rolling window of signals.
        </ParallaxHeading>
        <div className="landing-signals-grid">
          {SIGNALS.map((signal, index) => (
            <motion.article
              key={signal.title}
              className="landing-signal-card"
              initial={reduceMotion ? false : { opacity: 0, y: 22 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-40px" }}
              transition={{
                duration: 0.48,
                delay: reduceMotion ? 0 : index * 0.06,
                ease: [0.16, 1, 0.3, 1],
              }}
            >
              <h3>{signal.title}</h3>
              <p>{signal.body}</p>
            </motion.article>
          ))}
        </div>
        <p className="landing-signals-note">
          Low confidence means the system holds back rather than calling you tired
          when the camera cannot see you clearly. It says &ldquo;fatigue risk,&rdquo;
          never a medical diagnosis.
        </p>
      </div>
    </section>
  );
}
