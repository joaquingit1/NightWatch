# Nightwatch repository rules

These instructions apply to every agent and every file in this repository,
except where a more specific `AGENTS.md` adds stricter rules. Read
`nightwatch/LESSONS-2026-07-24.md` before changing robot behavior, navigation,
mapping, perception, streaming, operator controls, or the interaction pipeline.

## Non-negotiable product invariants

1. **One movement owner.** Effective velocity may come from exactly one owner:
   `safety > manual > escort > interaction/sleep_scan > exploration/patrol`.
   Social gestures never own the route.
2. **Manual means manual.** Manual Override is latched, immediate, and cannot be
   refused or automatically revoked by navigation, fatigue, interaction, or
   social behavior. Only safety may override it.
3. **Fail stopped manual commands.** Manual velocity commands require monotonic
   sequence numbers and timestamps. Send them at a fixed cadence and publish
   zero velocity within 500 ms after release, browser blur/hidden state,
   disconnect, stale input, or leaving Manual Override.
4. **Never create an unexplained hold.** Every stationary interval must expose
   an owner, reason, start time, hard deadline, and next transition. Outside an
   intentional sleep scan or human interaction, unexpected lack of progress
   must trigger recovery in at most 10 seconds.
5. **Sleep scans are bounded exceptions.** A sleep scan may stop the dog and
   lower its rear to aim the camera upward only in Sleep Analysis mode. It must
   retain the route goal and restore stance/resume on timeout, no detection,
   mode change, manual takeover, disconnect, or error.
6. **Do not invent hardware commands.** Check the local `dimos` source and the
   installed Unitree WebRTC driver before using a robot action or topic. Test
   uncertain contracts with a fake transport. Never probe an uncertain action
   by moving the live robot.
7. **Preserve A*.** Do not replace or bypass the current A* planner, local safety
   layer, dynamic-obstacle handling, glass/failure memory, stuck recovery,
   frontier scoring, focus/keep-out zones, or adaptive speed without a focused
   failing test and explicit task scope.
8. **Transient obstacles retain intent.** A moving person may cause a bounded
   local wait/dodge/replan to the same goal. Do not discard the global goal
   unless repeated evidence shows the region is persistently blocked.
9. **Maps are venue-scoped bundles.** Point cloud, occupancy data,
   relocalization metadata, semantic areas, sleeping areas, focus/keep-out
   zones, and display transform must be stored and loaded together by venue.
   Never overwrite another venue or mix local and map-frame coordinates.
10. **Do not fake map readiness.** The UI and API must distinguish file found,
    map loaded, and robot localized. Never show a saved map/zone as active until
    its frame is valid for the current run.
11. **Keep multiple sleeping areas.** Do not reduce semantic sleeping areas to a
    single `Bedroom`. Choose the nearest reachable confirmed area by A* path
    cost, with a clear operator override.
12. **Keep perception fast.** Capture once, keep the newest frame, perform
    inference at a controlled rate, cache annotations, and fan out results.
    Never add per-client inference, unbounded queues, duplicate encoding, or a
    blocking model call in robot command/navigation loops.
13. **Gate fatigue by mode/state.** It is off in Autonomous Exploration, active
    during Sleep Analysis patrol/scans, optionally display-only in Manual, and
    cannot start a second interaction during approach, intake, or escort.
14. **Public intake must bind to a session.** QR/NFC URLs require a signed,
    expiring interaction token. Submissions are idempotent and retryable; an
    unbound public response must never command the robot.
15. **Wave causes must be attributable.** Human-wave, sustained gaze, and
    ambient personality actions are separate triggers with evidence and
    cooldowns. Do not schedule random `Hello` actions that look like false
    detections.

## Change rules

- The current working tree is the source of truth. The
  `feature/operator-workbench-v2` robot stack is older: port operator UI,
  form/session, and state-machine features selectively; never merge it
  wholesale over current navigation or perception.
- The worktree may contain valuable uncommitted work. Preserve unrelated
  changes, inspect overlapping diffs, and make narrow patches.
- Keep ownership clear: mode/control arbitration belongs in one backend state
  machine; the browser requests commands and renders authoritative state but
  does not invent robot state.
- Prefer small vertical slices with adjacent tests. Do not mix UI restyling,
  navigation changes, model tuning, and hardware behavior in one patch.
- Do not remove, skip, loosen, or rewrite an existing passing test merely to
  make new code pass.
- Any new hold, retry, queue, timer, or background task must be bounded,
  cancellable, observable, and cleaned up on shutdown or mode change.
- Mode changes are transactional: stop the prior owner, confirm neutral output,
  transition state, then enable the new owner. Stale commands from the old epoch
  are rejected.
- Robot actions and TTS must not block the command watchdog, camera capture,
  navigation, or API event loop.
- Keep operator Emergency Stop and Manual Override available without scrolling
  on the supported booth viewport.

## Required checks before handoff

- Run focused unit/contract tests for every touched subsystem.
- Run the full existing Nightwatch and server suites; run client typecheck,
  lint, and relevant browser tests for UI changes.
- Exercise fake/offline transitions for manual takeover from every autonomous
  state, scan timeout/error cleanup, intake timeout/accept/decline, and escort
  completion.
- Report any check that could not run. “Compiles” is not proof that hardware
  behavior is correct.
