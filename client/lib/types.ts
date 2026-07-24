export interface FatigueFactors {
  perclos: number;
  blink_ms_p50: number;
  blink_ms_p90: number;
  nod_count: number;
  yawn_count: number;
  slump_deg: number;
  eye_cnn_perclos: number;
  movement_entropy: number;
  sedentary_hours: number;
}

export interface FatigueFrame {
  ts: number;
  person_id: string | null;
  bbox: [number, number, number, number];
  score: number;
  confidence: number;
  factors: FatigueFactors;
  calib_state: "uncalibrated" | "quick" | "full";
  scorer: string;
}

export interface PolicyEvent {
  ts: number;
  state: string;
  target_person: string | null;
  utterance: string | null;
  detail: string;
}

export interface PlanStop {
  tag: string;
  eta_s: number;
  kind: "nap" | "patrol" | "home";
}

export interface PlanResponse {
  stops: PlanStop[];
  updated_ts: number;
}

export interface LedgerEvent {
  ts: number;
  state: string;
  person_id: string | null;
  detail: string;
}

export interface LedgerResponse {
  events: LedgerEvent[];
  nap_count: number;
  pass_count: number;
}

export interface LeaderboardEntry {
  name_alias: string;
  peak_score: number;
  nap_count: number;
}

export interface LeaderboardResponse {
  entries: LeaderboardEntry[];
}
