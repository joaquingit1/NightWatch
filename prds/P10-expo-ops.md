# P10 — Expo Operations

Objective: operate a safe, alive robot whose default behavior demonstrates the product.

## Preflight

- battery above 60%, charge/stop threshold agreed;
- robot LAN below 10 ms and 0% observed loss;
- camera, LiDAR, costmap, Rerun, model, NFC relay, UI, and e-stop checked;
- premap relocalization accepted and `rest_area` reachable;
- speaker route/audibility status known; silent UI/NFC flow ready;
- taped operating zone and operator assigned.

## Live behavior

The dog begins in curiosity—mapping if needed, coverage patrol otherwise. A judge does
not wait for a reset. The operator UI shows the current behavior and why it moves.

For the product demonstration:

1. inject or obtain a consented fatigue candidate;
2. show confidence gating and target lock;
3. let the robot approach and offer help;
4. scan NFC/QR and accept;
5. escort to `rest_area`;
6. show automatic return to curiosity;
7. demonstrate manual preemption once if safe.

## Non-negotiables

- one operator holds manual/e-stop control;
- no autonomous motion in an untaped dense crowd;
- no unconsented named identification or close-up recording;
- no claim that a fatigue score is a medical diagnosis;
- stop autonomous operation at the battery threshold;
- if a subsystem fails, expose the hold/fallback honestly rather than hiding it.

## Acceptance

- The robot is active or explicitly held with a visible reason throughout the expo.
- At least one complete consented loop is logged and filmed.
- No safety incident, stale NFC movement, wrong-person intervention, or unexplained
  frozen state occurs.
