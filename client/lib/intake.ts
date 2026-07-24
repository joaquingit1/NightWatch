export interface IntakeOption {
  value: boolean | string;
  label_zh: string;
  label_en: string;
}

export interface IntakeQuestion {
  id: string;
  prompt_zh: string;
  prompt_en: string;
  options: IntakeOption[];
}

export interface IntakeSchema {
  session_id: string;
  questions: IntakeQuestion[];
}

export interface IntakeSubmitPayload {
  session_id: string;
  consent_analysis: boolean;
  tiredness: "energized" | "tired";
  wants_escort: boolean;
  name_alias?: string | null;
}

export interface IntakeResponse {
  response_id: string;
  session_id: string;
  created_ts: number;
  consent_analysis: boolean;
  tiredness: "energized" | "tired";
  wants_escort: boolean;
  status: string;
  name_alias: string | null;
  source: string;
  routing_hint: "escort" | "observe" | "declined";
}

export async function fetchIntakeSchema(sessionId?: string): Promise<IntakeSchema> {
  const query = sessionId ? `?s=${encodeURIComponent(sessionId)}` : "";
  const res = await fetch(`/api/form/schema${query}`, { cache: "no-store" });
  if (!res.ok) throw new Error("failed to load form schema");
  return res.json() as Promise<IntakeSchema>;
}

export async function submitIntake(payload: IntakeSubmitPayload): Promise<IntakeResponse> {
  const res = await fetch("/api/form/responses", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(detail || "submission failed");
  }
  return res.json() as Promise<IntakeResponse>;
}

export async function fetchPendingEscort(): Promise<IntakeResponse[]> {
  const res = await fetch("/api/form/responses/pending-escort", { cache: "no-store" });
  if (!res.ok) return [];
  const data = (await res.json()) as { responses: IntakeResponse[] };
  return data.responses ?? [];
}

export async function acknowledgeIntake(responseId: string): Promise<void> {
  await fetch(`/api/form/responses/${responseId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "acknowledged" }),
  });
}
