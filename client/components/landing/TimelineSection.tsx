"use client";

import { motion, useReducedMotion } from "framer-motion";

import { ParallaxHeading } from "./ParallaxHeading";
import { useLandingLanguage } from "./LandingLanguage";

export function TimelineSection() {
  const reduceMotion = useReducedMotion();
  const { copy } = useLandingLanguage();

  return (
    <section className="landing-section landing-timeline">
      <div className="landing-section-inner">
        <ParallaxHeading kicker={copy.timeline.kicker}>
          {copy.timeline.heading}
        </ParallaxHeading>
        <ol className="landing-timeline-list">
          {copy.timeline.moments.map((moment, index) => (
            <motion.li
              key={moment.num}
              initial={reduceMotion ? false : { opacity: 0, x: 18 }}
              whileInView={{ opacity: 1, x: 0 }}
              viewport={{ once: true, margin: "-40px" }}
              transition={{
                duration: 0.42,
                delay: reduceMotion ? 0 : index * 0.04,
                ease: [0.16, 1, 0.3, 1],
              }}
            >
              <div className="landing-step-label">
                <span>{moment.num}</span>
                <strong>{moment.title}</strong>
              </div>
              <p>{moment.body}</p>
            </motion.li>
          ))}
        </ol>
      </div>
    </section>
  );
}
