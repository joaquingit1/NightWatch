# P7 — Full Integration and Replay Regression

Objective: prove every subsystem composes without the LLM becoming the orchestrator.

## Work

1. Run the curiosity supervisor with mocked model and NFC events.
2. Run the live model with mock motion.
3. Run the complete stack in DimensionalOS replay against a saved map.
4. Run the complete stack on hardware above 60% battery in a taped safe zone.
5. Add regression assertions for behavior transitions, lease ownership, hold reasons,
   target ID, destination, and return-to-curiosity time.
6. Measure CPU, memory, camera freshness, LiDAR cadence, model latency, and navigation
   outcome together.

## Acceptance

- All replay scenarios pass ten consecutive times.
- No scenario has two autonomous motion owners.
- A crash in model, web/NFC, or LLM leaves safety and curiosity supervisor alive.
- Physical run completes three consecutive intervention/escort cycles.
- Saved-map restart and rest-area navigation pass on hardware.

## Cut policy

Model complexity and longitudinal identity are reduced before weakening behavior
supervision or consent.
