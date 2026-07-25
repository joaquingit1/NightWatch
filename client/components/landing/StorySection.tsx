"use client";

import { motion, useReducedMotion } from "framer-motion";

import { ParallaxHeading } from "./ParallaxHeading";

const rise = {
  hidden: { opacity: 0, y: 28 },
  visible: { opacity: 1, y: 0 },
};

export function StorySection() {
  const reduceMotion = useReducedMotion();

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
        <ParallaxHeading kicker="THE IDEA">
          An emotional support, autonomous dog.
        </ParallaxHeading>
        <div className="landing-story-grid">
          <p>
            A Unitree Go2 robot dog roams wherever people push themselves too
            hard — a hackathon floor, an office at 2am, a study hall during
            finals — reads signs of fatigue from a camera, and offers rest
            instead of another caffeine hit. If you accept, it walks you to a
            nap zone, keeps watch, and wakes you on schedule.
          </p>
          <p>
            Everything runs locally on laptops over a private network. No cloud
            required for the live demo. Curiosity is the default state —
            mapping, patrolling, and observing happen before anyone asks the dog
            to do anything.
          </p>
        </div>
      </motion.div>
    </section>
  );
}
