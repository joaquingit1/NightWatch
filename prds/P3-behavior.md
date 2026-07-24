# P3 — Sleep-Deprivation Model Runtime Contract

Objective: integrate the other developer's model without coupling robot behavior to its
internal architecture.

## Work

1. Model owner publishes `FatigueAssessment` exactly as frozen in P0.
2. Add image-quality, face-size, observation-duration, and confidence gates.
3. Run person detection continuously but schedule expensive scoring within a fixed CPU
   budget so camera/navigation remain responsive.
4. Add assessment cooldown, deduplication, and persistence of derived results only.
5. Build a replay publisher for scripted scores and recorded model outputs.
6. Display model version, confidence, observation time, and factors; never hide
   abstention.
7. Write threshold policy configuration separately from model code.
8. Support shadow assessments during `MAPPING`, but publish actionable candidates only
   when the map lifecycle reports operational `MAPPED`.

## Acceptance

- Scripted assessments produce deterministic actionable/non-actionable decisions.
- Low quality, low confidence, too-short observation, cooldown, and stale track each
  abstain.
- Model failure does not stop curiosity or navigation.
- With the live model enabled, camera freshness and map cadence remain within their
  measured budgets.
- Every intervention candidate can be traced to one assessment and model version.
- Shadow-mode outputs cannot create a person ledger entry or move the robot.

## Language

The product reports fatigue risk or signs of tiredness. It does not diagnose sleep
deprivation or make a medical claim.
