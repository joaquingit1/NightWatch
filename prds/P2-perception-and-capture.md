# P2 — Mapping Lifecycle, Persistence, and Coverage Patrol

Objective: turn curiosity from endless frontier retries into a floor-aware lifecycle.

## Work

1. Expose structured frontier-explorer status: active goal, goals reached, failures,
   reachable frontier count, and completion candidate.
2. Implement stable map-completion evaluation across multiple poses and attempts.
3. Require `home` and `rest_area` tags before declaring the operational map ready.
4. Keep the existing low-bandwidth map recorder and export smoke test.
5. Add DimensionalOS `RelocalizationModule` to the Night Watch blueprint behind a
   configured premap path.
6. Test saved-map relocalization in replay before using it on hardware.
7. Build a coverage patrol router using least-recently visited safe waypoints.
8. Revoke `MAPPED` and return to exploration when meaningful new frontiers appear.
9. Publish the operational gate that enables Night Watch monitoring only after
   `MAPPED`, tags, export, and localization requirements pass.

## Acceptance

- An incomplete replay stays in `MAPPING`; one transient no-frontier result does not
  complete it.
- A complete replay reaches `MAPPED`, writes/exports the map, and starts coverage patrol.
- Restart with the exported premap reaches accepted relocalization before stored-goal
  navigation is enabled.
- Patrol visits all configured waypoints within the test horizon without repeating one
  indefinitely.
- New reachable space moves the phase back to `MAPPING`.
- Returning to `MAPPING` disables new interventions while preserving active safety and
  explicit user tasks.

## Guardrails

Map saving is not map reuse. Do not claim persistence across restarts until replay and
live relocalization both pass.
