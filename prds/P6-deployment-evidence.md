# P6 — Operator UI and Evidence

Objective: make autonomous state and safety legible and capture truthful evidence.

## UI requirements

- latest-only camera feed and Rerun map;
- current behavior owner and remaining lease;
- movement expected, last odometry motion, and hold reason;
- map phase, frontier/patrol target, and relocalization confidence;
- current anonymous track and fatigue assessment confidence/factors;
- intervention/NFC request state;
- battery, connection health, manual override, and e-stop;
- event timeline export.

## Evidence requirements

1. Record replay fixtures for liveness, mapping completion, model abstention, target
   locking, NFC outcomes, escort, manual stop, and return to curiosity.
2. Capture actual latency/cadence numbers rather than PRD targets presented as results.
3. Film one complete autonomous loop and one safety-preemption loop.
4. Keep consented close-up footage separate from wide operational footage.

## Acceptance

- The operator can explain any three-second hold from the UI alone.
- Killing/restarting the web surface does not stop robot safety or curiosity.
- Feed reconnects without showing a stale image as live.
- Evidence artifacts match the event log and contain no fabricated ledger entries.
