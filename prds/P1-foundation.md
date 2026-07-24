# P1 — Curiosity Kernel and Three-Second Liveness

Objective: make curiosity the base state and the most reliable part of the robot.

## Work

1. Replace `IdleBehavior` with a deterministic `CuriositySupervisor`.
2. Start curiosity immediately after sensor/localization readiness; remove the
   90-second idle threshold and arbitrary 45–90-second roaming bursts.
3. Keep curiosity active while the agent thinks. Only a granted motion lease preempts
   it.
4. Implement priority leases, expiry, cancellation, and automatic fallback.
5. Track command and odometry heartbeats. If expected movement produces no translation
   or yaw for three seconds, declare `STUCK`, cancel the goal, and run bounded recovery.
6. Emit explicit hold reasons and never use motion as the response to an unsafe scene.
7. Route every autonomous velocity through MovementManager.
8. Preserve identity-locked curious follow, but cap it and make it lower priority than
   intervention/escort/explicit tasks.

## Acceptance

- Replay/mock run: 30 minutes with no unexplained still interval over three seconds.
- Ten task completion/failure/timeout cases resume curiosity within one second.
- Agent busy/thinking signals do not stop ongoing safe curiosity.
- Manual input preempts within 250 ms and curiosity does not resume until the manual
  lease releases.
- Low battery, missing costmap, close obstacle, and explicit stay each produce a zero
  velocity plus the correct hold reason.
- A stuck simulated goal triggers bounded recovery rather than endless replanning.

## Cut line

P2 and all Night Watch behavior are blocked until this phase passes.
