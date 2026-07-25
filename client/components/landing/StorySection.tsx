"use client";

import { motion, useReducedMotion } from "framer-motion";

import { ParallaxHeading } from "./ParallaxHeading";
import { useLandingLanguage } from "./LandingLanguage";

const rise = {
  hidden: { opacity: 0, y: 28 },
  visible: { opacity: 1, y: 0 },
};

export function StorySection() {
  const reduceMotion = useReducedMotion();
  const { copy } = useLandingLanguage();

  return (
    <section className="landing-section landing-story">
      <motion.div
        className="landing-section-inner"
        initial={reduceMotion ? false : "hidden"}
        whileInView="visible"
        viewport={{ once: true, margin: "-80px" }}
        variants={rise}
        transition={{ duration: 0.62, ease: [0.16, 1, 0.3, 1] }}
      >
        <ParallaxHeading kicker={copy.story.kicker}>{copy.story.heading}</ParallaxHeading>
        <div className="landing-story-grid">
          {copy.story.body.map((paragraph) => (
            <p key={paragraph}>{paragraph}</p>
          ))}
        </div>
      </motion.div>
    </section>
  );
}
