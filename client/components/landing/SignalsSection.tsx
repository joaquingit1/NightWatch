"use client";

import { motion, useReducedMotion } from "framer-motion";

import { ParallaxHeading } from "./ParallaxHeading";
import { useLandingLanguage } from "./LandingLanguage";

export function SignalsSection() {
  const reduceMotion = useReducedMotion();
  const { copy } = useLandingLanguage();

  return (
    <section className="landing-section landing-signals">
      <div className="landing-section-inner">
        <ParallaxHeading kicker={copy.signals.kicker}>
          {copy.signals.headingLine1}
          <br />
          {copy.signals.headingLine2}
        </ParallaxHeading>
        <div className="landing-signals-grid">
          {copy.signals.items.map((signal, index) => (
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
        <p className="landing-signals-note">{copy.signals.note}</p>
      </div>
    </section>
  );
}
