"use client";

import Link from "next/link";
import { motion, useReducedMotion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";

const rise = {
  hidden: { opacity: 0, y: 24 },
  visible: { opacity: 1, y: 0 },
};

export function Hero() {
  const reduceMotion = useReducedMotion();
  const heroRef = useRef<HTMLElement>(null);

  const { scrollYProgress } = useScroll({
    target: heroRef,
    offset: ["start start", "end start"],
  });

  const brandY = useTransform(scrollYProgress, [0, 1], reduceMotion ? [0, 0] : [0, -40]);
  const copyY = useTransform(scrollYProgress, [0, 1], reduceMotion ? [0, 0] : [0, -70]);
  const actionsY = useTransform(scrollYProgress, [0, 1], reduceMotion ? [0, 0] : [0, -100]);
  const contentOpacity = useTransform(scrollYProgress, [0, 0.8], [1, 0]);

  return (
    <section id="landing-hero" ref={heroRef} className="landing-hero">
      <motion.div className="landing-hero-content" style={{ opacity: contentOpacity }}>
        <motion.header
          className="landing-brand"
          style={{ y: brandY }}
          initial={reduceMotion ? false : "hidden"}
          animate="visible"
          variants={rise}
          transition={{ duration: 0.56, ease: [0.16, 1, 0.3, 1] }}
        >
          <h1 className="landing-brand-zh">守夜犬</h1>
          <p className="landing-brand-en">NIGHT&nbsp; WATCH</p>
        </motion.header>

        <motion.div
          className="landing-hero-copy"
          style={{ y: copyY }}
          initial={reduceMotion ? false : "hidden"}
          animate="visible"
          variants={rise}
          transition={{ duration: 0.62, delay: 0.08, ease: [0.16, 1, 0.3, 1] }}
        >
          <p className="landing-hero-tagline">
            Every AI makes you work more.
            <br />
            <strong>This one makes you stop.</strong>
          </p>
          <p className="landing-hero-sub">
            每个 AI 都想让你更努力。它想让你休息。
            <br />
            Autonomous patrol · fatigue triage · guided rest
          </p>
        </motion.div>

        <motion.nav
          className="landing-hero-actions"
          style={{ y: actionsY }}
          initial={reduceMotion ? false : "hidden"}
          animate="visible"
          variants={rise}
          transition={{ duration: 0.62, delay: 0.16, ease: [0.16, 1, 0.3, 1] }}
        >
          <Link href="/form" className="landing-action landing-action--primary">
            Report a nap
            <span aria-hidden="true">→</span>
          </Link>
          <Link href="/lidar" className="landing-action landing-action--secondary">
            Watch it patrol
            <span aria-hidden="true">↗</span>
          </Link>
          <Link href="/booth" className="landing-action-tertiary">
            Operator console
          </Link>
        </motion.nav>

        <motion.div
          className="landing-hero-spec"
          style={{ y: actionsY }}
          initial={reduceMotion ? false : "hidden"}
          animate="visible"
          variants={rise}
          transition={{ duration: 0.62, delay: 0.22, ease: [0.16, 1, 0.3, 1] }}
        >
          <p>UNITREE GO2</p>
          <span aria-hidden="true" />
          <p>DIMENSIONALOS · ADVENTUREX 2026</p>
        </motion.div>
      </motion.div>
    </section>
  );
}
