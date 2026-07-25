# P4 — Anonymous Person Ledger and Presence Signals

Objective: recognize repeat encounters conservatively without making facial
surveillance part of the MVP.

## Work

1. Store anonymous session IDs, tracker IDs, time/position, re-ID confidence, fatigue
   assessments, interventions, and cooldowns.
2. Use tracker plus appearance embeddings for short/medium-term re-identification.
3. Expire uncertain identities and merge only above a measured threshold.
4. Implement opt-in alias association through the NFC/QR flow.
5. Implement two-hour presence checkpoints: at most one increment per person per
   eligible two-hour interval.
6. Implement conservative extended-presence rules:
   - 15-hour span requires supporting sightings throughout the interval;
   - five-hour span plus last sighting after 03:00 raises late-night priority.
7. Add delete/expire controls and default raw-image non-retention.
8. Keep face embeddings/named recognition behind a disabled stretch flag.
9. Create longitudinal records only while the map lifecycle is operational `MAPPED`;
   transient mapping-phase tracks are discarded.

## Acceptance

- The same test subject across a two-hour replay gets one checkpoint, not one per frame.
- Different subjects are not merged in the multi-person test set above the allowed
  false-match budget.
- A first and last timestamp with a long evidence gap does not count as continuous
  presence.
- Decline/delete removes the alias association and suppresses repeat prompts for the
  configured cooldown.
- The MVP works with anonymous IDs when re-identification is unavailable.

## Cut line

If P5 is at risk, ship only same-track cooldown and opt-in aliases. Longitudinal
recognition is secondary to the complete intervention loop.
