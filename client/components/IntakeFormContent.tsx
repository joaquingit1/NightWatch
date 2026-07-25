"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { IntakeQuestion } from "@/components/IntakeQuestion";
import {
  fetchIntakeSchema,
  submitIntake,
  type IntakeQuestion as IntakeQuestionType,
  type IntakeResponse,
} from "@/lib/intake";

type Step =
  | "loading"
  | "waiting"
  | "questions"
  | "nickname"
  | "submitting"
  | "done"
  | "error";

function getSessionFromUrl(searchParams: URLSearchParams): string | undefined {
  const raw = searchParams.get("s");
  return raw && raw.length >= 8 ? raw : undefined;
}

function randomSubmissionId(): string {
  // crypto.randomUUID only exists in secure contexts; phones open this form
  // over plain http://<lan-ip>:3000, so fall back to a uuid4-shaped id built
  // from getRandomValues (available on http) or Math.random as a last resort.
  const cryptoApi = typeof window !== "undefined" ? window.crypto : undefined;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    return cryptoApi.randomUUID().replaceAll("-", "");
  }
  const bytes = new Uint8Array(16);
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    cryptoApi.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function IntakeFormContent() {
  const searchParams = useSearchParams();
  const [step, setStep] = useState<Step>("loading");
  const [sessionId, setSessionId] = useState("");
  const [submissionId, setSubmissionId] = useState("");
  const [interactionId, setInteractionId] = useState<string | null>(null);
  const [interactionToken, setInteractionToken] = useState<string | null>(null);
  const [questions, setQuestions] = useState<IntakeQuestionType[]>([]);
  const [questionIndex, setQuestionIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, boolean | string>>({});
  const [nameAlias, setNameAlias] = useState("");
  const [result, setResult] = useState<IntakeResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    let active = true;
    let retry: number | null = null;
    const requestedSession = getSessionFromUrl(searchParams);
    const load = async () => {
      try {
        const schema = await fetchIntakeSchema(requestedSession);
        if (!active) return;
        if (!schema.interaction_active) {
          setStep("waiting");
          retry = window.setTimeout(load, 2000);
          return;
        }
        setSessionId(schema.session_id);
        setSubmissionId((current) => current || randomSubmissionId());
        setInteractionId(schema.interaction_id ?? null);
        setInteractionToken(schema.interaction_token ?? null);
        setQuestions(schema.questions);
        setStep("questions");
      } catch {
        if (active) {
          setStep("waiting");
          retry = window.setTimeout(load, 2000);
        }
      }
    };
    load();
    return () => {
      active = false;
      if (retry !== null) window.clearTimeout(retry);
    };
  }, [searchParams]);

  const currentQuestion = questions[questionIndex];

  const submitForm = useCallback(
    async (finalAnswers: Record<string, boolean | string>, alias: string | null) => {
      setStep("submitting");
      try {
        const response = await submitIntake({
          submission_id: submissionId,
          session_id: sessionId,
          interaction_id: interactionId,
          interaction_token: interactionToken,
          tiredness: (finalAnswers.tiredness as "energized" | "tired") ?? "energized",
          wants_escort: Boolean(finalAnswers.wants_escort),
          name_alias: alias,
        });
        setResult(response);
        setStep("done");
      } catch {
        setErrorMessage("提交失败，请重试。");
        setStep("error");
      }
    },
    [interactionId, interactionToken, sessionId, submissionId],
  );

  const handleSelect = (value: boolean | string) => {
    if (!currentQuestion) return;
    const nextAnswers = { ...answers, [currentQuestion.id]: value };
    setAnswers(nextAnswers);

    window.setTimeout(() => {
      if (questionIndex + 1 >= questions.length) {
        setStep("nickname");
        return;
      }
      setQuestionIndex((index) => index + 1);
    }, 280);
  };

  const progressDots = useMemo(() => {
    if (step === "nickname") return questions.length;
    return questionIndex;
  }, [questionIndex, questions.length, step]);

  return (
    <main className="min-h-screen bg-booth-bg px-4 py-8">
      <div className="mx-auto flex w-full max-w-lg flex-col gap-6">
        <header className="text-center space-y-2 animate-slide-up">
          <h1 className="text-3xl font-extrabold tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-booth-text to-booth-accent">
            守夜犬 <span className="text-booth-accent font-light">Night Watch</span>
          </h1>
          <p className="text-sm text-booth-muted">休息登记 · Rest intake</p>
        </header>

        <section className="glass-panel flex min-h-[420px] flex-col justify-between rounded-2xl p-6 sm:p-8">
          {step === "loading" && (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 text-booth-muted">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-booth-accent border-t-transparent" />
              <p className="text-sm">加载中...</p>
            </div>
          )}

          {step === "waiting" && (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center text-booth-muted">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-booth-accent border-t-transparent" />
              <h2 className="text-xl font-bold text-booth-text">等待机器狗发起互动</h2>
              <p className="max-w-xs text-sm">
                Stay near Night Watch. This page will open automatically when
                the dog is ready for your response.
              </p>
            </div>
          )}

          {step === "questions" && currentQuestion && (
            <>
              <div className="mb-6 flex justify-center gap-2">
                {questions.map((question, index) => (
                  <span
                    key={question.id}
                    className={`h-2 w-2 rounded-full transition-colors ${
                      index <= progressDots ? "bg-booth-accent" : "bg-slate-200"
                    }`}
                  />
                ))}
              </div>
              <IntakeQuestion
                promptZh={currentQuestion.prompt_zh}
                promptEn={currentQuestion.prompt_en}
                options={currentQuestion.options.map((option) => ({
                  value: option.value,
                  labelZh: option.label_zh,
                  labelEn: option.label_en,
                }))}
                onSelect={handleSelect}
                selected={answers[currentQuestion.id] ?? null}
              />
            </>
          )}

          {step === "nickname" && (
            <div className="animate-slide-up space-y-6 text-center">
              <div className="space-y-2">
                <h2 className="text-2xl font-bold text-booth-text">怎么称呼你？</h2>
                <p className="text-sm text-booth-muted">Optional nickname for the dog</p>
              </div>
              <input
                type="text"
                value={nameAlias}
                onChange={(event) => setNameAlias(event.target.value)}
                placeholder="昵称 / nickname"
                maxLength={32}
                className="w-full rounded-xl border border-booth-border bg-white px-4 py-3 text-center text-lg outline-none focus:border-booth-accent focus:ring-2 focus:ring-booth-accent/20"
              />
              <div className="flex flex-col gap-3">
                <button
                  type="button"
                  onClick={() => submitForm(answers, nameAlias.trim() || null)}
                  className="rounded-xl bg-booth-accent px-4 py-3 text-sm font-semibold text-white transition hover:bg-blue-600"
                >
                  提交 Submit
                </button>
                <button
                  type="button"
                  onClick={() => submitForm(answers, null)}
                  className="rounded-xl border border-booth-border px-4 py-3 text-sm font-medium text-booth-muted transition hover:bg-slate-50"
                >
                  跳过 Skip
                </button>
              </div>
            </div>
          )}

          {step === "submitting" && (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 text-booth-muted">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-booth-accent border-t-transparent" />
              <p className="text-sm">提交中...</p>
            </div>
          )}

          {step === "done" && result && (
            <div className="animate-slide-up flex flex-1 flex-col items-center justify-center gap-4 text-center">
              <div className="flex h-16 w-16 items-center justify-center rounded-full bg-emerald-100 text-emerald-600">
                <svg className="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              </div>
              {result.routing_hint === "escort" ? (
                <>
                  <h2 className="text-2xl font-bold text-booth-text">护送请求已收到</h2>
                  <p className="text-sm text-booth-muted">
                    Please wait for the booth operator to confirm that Night Watch is ready.
                  </p>
                </>
              ) : result.routing_hint === "observe" ? (
                <>
                  <h2 className="text-2xl font-bold text-booth-text">感谢参与</h2>
                  <p className="text-sm text-booth-muted">
                    We will keep an eye on you. Rest when you need to.
                  </p>
                </>
              ) : (
                <>
                  <h2 className="text-2xl font-bold text-booth-text">已记录</h2>
                  <p className="text-sm text-booth-muted">Thanks. No analysis will be performed.</p>
                </>
              )}
            </div>
          )}

          {step === "error" && (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center">
              <p className="text-booth-danger">{errorMessage}</p>
              <button
                type="button"
                onClick={() =>
                  submitForm(answers, nameAlias.trim() || null)
                }
                className="rounded-xl bg-booth-accent px-4 py-2 text-sm font-semibold text-white"
              >
                重试 Retry
              </button>
            </div>
          )}
        </section>

        <p className="text-center text-xs text-booth-muted">
          扫码参与 · Scan to join · No login required
        </p>
      </div>
    </main>
  );
}
