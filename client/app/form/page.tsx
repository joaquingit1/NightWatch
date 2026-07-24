"use client";

import Image from "next/image";
import { Suspense } from "react";
import { IntakeFormContent } from "@/components/IntakeFormContent";

function FormLoading() {
  return (
    <main className="intake-page">
      <Image
        alt=""
        aria-hidden="true"
        className="intake-background-layer"
        fill
        priority
        sizes="100vw"
        src="/form/nightwatch-paper-bg.png"
      />
      <div className="intake-poster intake-poster--loading">
        <header className="intake-brand">
          <h1 className="intake-brand-zh">守夜犬</h1>
          <p className="intake-brand-en">NIGHT&nbsp; WATCH</p>
        </header>
        <section className="intake-panel intake-loading-panel">
          <div className="intake-loader" aria-hidden="true" />
          <p>加载中 / LOADING</p>
        </section>
      </div>
    </main>
  );
}

export default function IntakeFormPage() {
  return (
    <Suspense fallback={<FormLoading />}>
      <IntakeFormContent />
    </Suspense>
  );
}
