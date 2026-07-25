"use client";

import { motion, useReducedMotion } from "framer-motion";

import { ParallaxHeading } from "./ParallaxHeading";

const MOMENTS = [
  {
    num: "01",
    title: "Patrol",
    body: "The dog moves through a mapped corridor on its own.",
  },
  {
    num: "02",
    title: "Triage",
    body: "A live fatigue score (0–100) appears on the booth screen.",
  },
  {
    num: "03",
    title: "Diagnose",
    body: "The dog pauses, takes a short reading, and explains what it sees.",
  },
  {
    num: "04",
    title: "Prescribe",
    body: "It recommends a nap in a warm, pre-recorded voice.",
  },
  {
    num: "05",
    title: "Escort",
    body: "It leads you to the mattress at a careful pace.",
  },
  {
    num: "06",
    title: "Nap ledger",
    body: "Arrival registers the nap and starts a visible wake-check countdown.",
  },
  {
    num: "07",
    title: "Expression",
    body: "A hand cover queues a safe dog gesture without cancelling navigation.",
  },
  {
    num: "08",
    title: "Posture",
    body: "Lie down holds indefinitely; Stand releases the hold and resumes safely.",
  },
] as const;

export function TimelineSection() {
  const reduceMotion = useReducedMotion();

  return (
    <section className="landing-section landing-timeline">
      <div className="landing-section-inner">
        <ParallaxHeading kicker="WHAT YOU WILL SEE">The closed physical loop</ParallaxHeading>
        <ol className="landing-timeline-list">
          {MOMENTS.map((moment, index) => (
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
