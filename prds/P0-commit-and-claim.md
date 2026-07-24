# P0 — Freeze Reality, Contracts, and Capability Probes

Objective: remove speculative assumptions before more behavior code is written.

## Work

1. Add the frozen contracts from `00-OVERVIEW.md`.
2. Add a capability registry populated by live probes:
   camera, LiDAR, odometry, navigation, manual stop, speaker route, microphone stream,
   internet relay, NFC page, tagged location, map export, and relocalization.
3. Record each capability as `verified`, `degraded`, `unverified`, or `unavailable`,
   with timestamp and evidence.
4. Replace “idle behavior” terminology with “curiosity” in public/API descriptions.
5. Create one replay fixture and one mock motion facade so P1 can run without the dog.
6. Assign the robot, model, and product owners and record the handoff interfaces.

## Acceptance

- Contracts import and serialize round-trip.
- The capability report reflects current truth: no verified microphone; speaker route
  and venue audibility are separate checks; relocalization is not yet integrated.
- Existing Nightwatch regression tests remain green.
- No PRD claims an unverified capability as present.

## Do not

- Train a model.
- Build the final UI.
- Change motion behavior before the supervisor contract exists.
