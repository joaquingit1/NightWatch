"use client";

interface IntakeQuestionProps {
  promptZh: string;
  promptEn: string;
  options: Array<{
    value: boolean | string;
    labelZh: string;
    labelEn: string;
  }>;
  onSelect: (value: boolean | string) => void;
  selected?: boolean | string | null;
}

export function IntakeQuestion({
  promptZh,
  promptEn,
  options,
  onSelect,
  selected = null,
}: IntakeQuestionProps) {
  return (
    <div className="animate-slide-up space-y-6">
      <div className="text-center space-y-2 px-2">
        <h2 className="text-2xl font-bold text-booth-text leading-snug">{promptZh}</h2>
        <p className="text-sm text-booth-muted">{promptEn}</p>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {options.map((option) => {
          const isSelected = selected === option.value;
          return (
            <button
              key={String(option.value)}
              type="button"
              onClick={() => onSelect(option.value)}
              className={`group relative flex min-h-[120px] flex-col items-center justify-center rounded-2xl border-2 px-4 py-6 text-center transition-all duration-200 active:scale-[0.98] ${
                isSelected
                  ? "border-booth-accent bg-blue-50 shadow-md shadow-booth-accent/10"
                  : "border-booth-border bg-white hover:border-booth-accent/50 hover:bg-blue-50/40"
              }`}
            >
              <span
                className={`mb-4 flex h-10 w-10 items-center justify-center rounded-full border-2 transition-colors ${
                  isSelected
                    ? "border-booth-accent bg-booth-accent text-white"
                    : "border-slate-300 bg-white text-transparent group-hover:border-booth-accent/60"
                }`}
              >
                {isSelected && (
                  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                )}
              </span>
              <span className="text-lg font-semibold text-booth-text">{option.labelZh}</span>
              <span className="mt-1 text-xs text-booth-muted">{option.labelEn}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
