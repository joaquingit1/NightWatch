"use client";

import { motion, useReducedMotion } from "framer-motion";

const TECH = [
  "Unitree Go2",
  "DimensionalOS",
  "YOLOv8-Face",
  "MediaPipe",
  "Local LLM",
  "FastAPI",
  "Next.js",
  "SQLite ledger",
] as const;

export function TechStripSection() {
  const reduceMotion = useReducedMotion();

  return (
    <section className="landing-section landing-tech">
      <motion.div
        className="landing-section-inner"
        initial={reduceMotion ? false : { opacity: 0, y: 20 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-60px" }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
      >
        <p className="landing-kicker">TECHNOLOGY</p>
        <ul className="landing-tech-list">
          {TECH.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </motion.div>
    </section>
  );
}
