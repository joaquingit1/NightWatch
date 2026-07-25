"use client";

import Link from "next/link";
import { motion, useReducedMotion, useScroll, useTransform } from "framer-motion";
import { useRef } from "react";

export function CtaFooter() {
  const reduceMotion = useReducedMotion();
  const footerRef = useRef<HTMLDivElement>(null);

  const { scrollYProgress } = useScroll({
    target: footerRef,
    offset: ["start end", "end start"],
  });
  const headingY = useTransform(scrollYProgress, [0, 1], reduceMotion ? [0, 0] : [24, -24]);

  return (
    <footer className="landing-footer">
      <motion.div
        ref={footerRef}
        className="landing-footer-inner"
        initial={reduceMotion ? false : { opacity: 0, y: 24 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: "-60px" }}
        transition={{ duration: 0.56, ease: [0.16, 1, 0.3, 1] }}
      >
        <motion.h2 className="landing-footer-heading" style={{ y: headingY }}>
          Ask for the live loop.
          <br />
          We&apos;ll show you the ledger.
        </motion.h2>
        <nav className="landing-footer-nav">
          <Link href="/form" className="landing-action landing-action--primary">
            Intake form
            <span aria-hidden="true">→</span>
          </Link>
          <Link href="/booth" className="landing-action landing-action--secondary">
            Booth console
            <span aria-hidden="true">↗</span>
          </Link>
          <Link href="/lidar" className="landing-action landing-action--ghost">
            Spatial map
          </Link>
        </nav>
        <p className="landing-footer-credit">
          Built at AdventureX 2026 · Theme: Reverse · #adventurex2026
        </p>
      </motion.div>
    </footer>
  );
}
