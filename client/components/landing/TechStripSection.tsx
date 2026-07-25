"use client";

import { motion, useReducedMotion } from "framer-motion";

import { useLandingLanguage } from "./LandingLanguage";

export function TechStripSection() {
  const reduceMotion = useReducedMotion();
  const { copy } = useLandingLanguage();

  return (
    <section className="landing-section landing-tech">
      <motion.div
        className="landing-section-inner"
        initial={reduceMotion ? false : { opacity: 0, y: 20 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-60px" }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
      >
        <p className="landing-kicker">{copy.tech.kicker}</p>
        <ul className="landing-tech-list">
          {copy.tech.items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </motion.div>
    </section>
  );
}
