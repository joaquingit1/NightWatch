# P8 — Field Hardening

Objective: discover venue failures before judges do.

## Test matrix

- crowded and empty corridor;
- close obstacle/person blocking the robot;
- glasses, low face angle, partial occlusion, multiple people;
- venue lighting and noise;
- Wi-Fi latency/loss and phone internet availability;
- stale NFC request and two simultaneous phone requests;
- low battery, WebRTC loss, relocalization rejection, no frontier, stuck planner;
- manual takeover during every motion state.

## Acceptance

- 30-minute curiosity soak with no unexplained still interval over three seconds.
- Ten full loops; final five need no developer intervention.
- Every unsafe condition becomes an explicit hold, not recovery motion.
- Camera has no growing delay during model inference and map updates.
- Fallback demo—recorded model event plus live robot behavior—can be entered in under
  60 seconds without claiming the model is live.

## Freeze

After this phase, accept only defects, measurement corrections, and submission work.
