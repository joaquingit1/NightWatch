"use client";

import { Suspense } from "react";
import { IntakeFormContent } from "@/components/IntakeFormContent";

function FormLoading() {
  return (
    <main className="min-h-screen bg-booth-bg px-4 py-8">
      <div className="mx-auto flex w-full max-w-lg flex-col gap-6">
        <header className="text-center space-y-2 animate-slide-up">
          <h1 className="text-3xl font-extrabold tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-booth-text to-booth-accent">
            守夜犬 <span className="text-booth-accent font-light">Night Watch</span>
          </h1>
          <p className="text-sm text-booth-muted">休息登记 · Rest intake</p>
        </header>
        <section className="glass-panel flex min-h-[420px] items-center justify-center rounded-2xl p-6">
          <div className="flex flex-col items-center gap-3 text-booth-muted">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-booth-accent border-t-transparent" />
            <p className="text-sm">加载中...</p>
          </div>
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
