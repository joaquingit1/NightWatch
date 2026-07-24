"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { IntakeQuestion } from "@/components/IntakeQuestion";
import {
  fetchIntakeSchema,
  submitIntake,
  type IntakeQuestion as IntakeQuestionType,
} from "@/lib/intake";

type Step = "loading" | "questions" | "submitting" | "done" | "error";
type Outcome = "coding" | "escort" | "rest";

function getSessionFromUrl(searchParams: URLSearchParams): string | undefined {
  const raw = searchParams.get("s");
  return raw && raw.length >= 8 ? raw : undefined;
}

export function IntakeFormContent() {
  const searchParams = useSearchParams();
  const [step, setStep] = useState<Step>("loading");
  const [sessionId, setSessionId] = useState("");
  const [questions, setQuestions] = useState<IntakeQuestionType[]>([]);
  const [questionIndex, setQuestionIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, boolean | string>>({});
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const schema = await fetchIntakeSchema(getSessionFromUrl(searchParams));
        if (!active) return;
        setSessionId(schema.session_id);
        setQuestions(schema.questions);
        setStep("questions");
      } catch {
        if (active) {
          setErrorMessage(
            "无法加载表单，请稍后重试。 / Unable to load the form. Please try again later.",
          );
          setStep("error");
        }
      }
    };
    load();
    return () => {
      active = false;
    };
  }, [searchParams]);

  const currentQuestion = questions[questionIndex];

  const submitForm = useCallback(
    async (finalAnswers: Record<string, boolean | string>, nextOutcome: Outcome) => {
      setStep("submitting");
      try {
        await submitIntake({
          session_id: sessionId,
          consent_analysis: false,
          tiredness: (finalAnswers.tiredness as "energized" | "tired") ?? "energized",
          wants_escort: Boolean(finalAnswers.wants_escort),
          name_alias: null,
        });
        setOutcome(nextOutcome);
        setStep("done");
      } catch {
        setErrorMessage("提交失败，请重试。 / Submission failed. Please try again.");
        setStep("error");
      }
    },
    [sessionId],
  );

  const handleSelect = (value: boolean | string) => {
    if (!currentQuestion) return;
    const nextAnswers = { ...answers, [currentQuestion.id]: value };
    setAnswers(nextAnswers);

    window.setTimeout(() => {
      if (currentQuestion.id === "tiredness" && value === "energized") {
        submitForm(nextAnswers, "coding");
        return;
      }
      if (currentQuestion.id === "wants_escort") {
        submitForm(nextAnswers, value === true ? "escort" : "rest");
        return;
      }
      setQuestionIndex((index) => index + 1);
    }, 280);
  };

  const progressDots = useMemo(() => questionIndex, [questionIndex]);

  const displayStep = Math.min(
    questions.length || 2,
    step === "submitting" || step === "done"
      ? questions.length || 2
      : questionIndex + 1,
  );
  const displayTotal = questions.length || 2;

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
      <div className="intake-paper-grain" aria-hidden="true" />
      <span className="intake-target intake-target--top" aria-hidden="true" />
      <span className="intake-target intake-target--bottom" aria-hidden="true" />

      <div className="intake-poster">
        <header className="intake-brand">
          <h1 className="intake-brand-zh">守夜犬</h1>
          <p className="intake-brand-en">NIGHT&nbsp; WATCH</p>
        </header>

        <section className={`intake-panel intake-panel--${step}`} aria-live="polite">
          <div className="intake-panel-topline">
            <span>休息登记 / REST INTAKE</span>
            <span>
              第 {String(displayStep).padStart(2, "0")} 题 / Q.{" "}
              {String(displayStep).padStart(2, "0")} /{" "}
              {String(displayTotal).padStart(2, "0")}
            </span>
          </div>

          {step === "loading" && (
            <div className="intake-state intake-state--center">
              <div className="intake-loader" aria-hidden="true" />
              <p>加载中 / LOADING</p>
            </div>
          )}

          {step === "questions" && currentQuestion && (
            <>
              <div className="intake-progress" aria-label={`Question ${questionIndex + 1} of ${questions.length}`}>
                {questions.map((question, index) => (
                  <span
                    key={question.id}
                    className={index <= progressDots ? "is-active" : ""}
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

          {step === "submitting" && (
            <div className="intake-state intake-state--center">
              <div className="intake-loader" aria-hidden="true" />
              <p>提交中 / SUBMITTING</p>
            </div>
          )}

          {step === "done" && outcome && (
            <div className="intake-state intake-state--done">
              <div className="intake-done-mark">
                <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              </div>
              {outcome === "coding" ? (
                <>
                  <h2>耶耶祝你 coding 顺利！</h2>
                  <p>Yeye wishes you smooth coding!</p>
                </>
              ) : outcome === "escort" ? (
                <>
                  <h2>好的，耶耶将带你去睡眠空间</h2>
                  <p>All right, Yeye will guide you to the sleep space.</p>
                </>
              ) : (
                <>
                  <h2>好的，耶耶提醒你注意休息哦～</h2>
                  <p>All right, Yeye reminds you to take care and get some rest.</p>
                </>
              )}
            </div>
          )}

          {step === "error" && (
            <div className="intake-state intake-state--error">
              <span className="intake-error-code">ERR / 503</span>
              <h2>连接中断 / CONNECTION LOST</h2>
              <p>{errorMessage}</p>
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="intake-action intake-action--primary"
              >
                <span>重试 / RETRY</span>
                <span aria-hidden="true">→</span>
              </button>
            </div>
          )}
        </section>

        <aside className="intake-machine-spec" aria-label="Robot platform">
          <p>UNITREE GO2 AIR</p>
          <span />
          <p>POWERED BY<br />DIMENSIONAL OS</p>
        </aside>

        <div className="intake-robot-layer" aria-hidden="true">
          <Image
            src="/form/nightwatch-robot-dog.png"
            alt=""
            width={1024}
            height={1536}
            priority
            sizes="(max-width: 520px) 145vw, (max-width: 840px) 132vw, 58vw"
          />
        </div>

        <footer className="intake-footer">
          <div className="intake-step-label">
            <span>休息登记 / REST INTAKE /</span>
            <strong>{String(displayStep).padStart(2, "0")}</strong>
            <i>—</i>
            <b>{String(displayTotal).padStart(2, "0")}</b>
          </div>
          <p>扫码 / 回答 / 休息 &nbsp; SCAN / ANSWER / REST</p>
        </footer>
      </div>
    </main>
  );
}
