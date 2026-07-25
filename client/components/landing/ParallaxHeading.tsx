"use client";

import { motion, useReducedMotion, useScroll, useTransform } from "framer-motion";
import { useRef, type ReactNode } from "react";

interface ParallaxHeadingProps {
  kicker: ReactNode;
  children: ReactNode;
  className?: string;
}

export function ParallaxHeading({ kicker, children, className }: ParallaxHeadingProps) {
  const ref = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();

  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start end", "end start"],
  });

  const y = useTransform(scrollYProgress, [0, 1], reduceMotion ? [0, 0] : [24, -24]);

  return (
    <motion.div ref={ref} className={className} style={{ y }}>
      <p className="landing-kicker">{kicker}</p>
      <h2 className="landing-heading">{children}</h2>
    </motion.div>
  );
}
