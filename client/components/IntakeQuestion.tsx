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
    <div className="intake-question">
      <div className="intake-question-copy">
        <h2>{promptZh}</h2>
        <p>{promptEn}</p>
      </div>
      <div className="intake-option-list">
        {options.map((option, index) => {
          const isSelected = selected === option.value;
          return (
            <button
              key={String(option.value)}
              type="button"
              onClick={() => onSelect(option.value)}
              aria-pressed={isSelected}
              className={`intake-option ${isSelected ? "is-selected" : ""}`}
            >
              <span className="intake-option-label">
                <strong>{option.labelZh}</strong>
                <span aria-hidden="true"> / </span>
                <b>{option.labelEn.toUpperCase()}</b>
              </span>
              <span className={`intake-option-arrow ${index === 0 ? "is-orange" : "is-blue"}`}>
                <svg viewBox="0 0 64 32" aria-hidden="true">
                  <path d="M1 16h54M42 3l13 13-13 13" />
                </svg>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
