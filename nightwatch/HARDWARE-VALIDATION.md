# Supervised hardware acceptance

Run this only with a powered robot, a clear focus area, and a person ready to
use the physical emergency stop. Automated and offline-browser tests cannot
prove traction, camera pitch direction, obstacle clearance, speaker volume, or
real venue relocalization.

## Preflight

1. Close old robot/operator processes and confirm ports 3000, 5555, 8000, and
   8001 are free.
2. Select the correct venue bundle and draw a conservative focus area plus all
   known keep-out hazards.
3. Start once with:
   `./run_integrated.sh --with-robot --venue <venue-name>`.
4. Confirm the workbench reports the selected venue, saved map loaded,
   localization state, fresh camera age, one authoritative operating mode, and
   no unexplained hold.

## Acceptance sequence

1. **Autonomous:** select Autonomous. After sensors become ready, the dog must
   acquire an exploration route without extra setup. It must remain within the
   focus area, preserve A* navigation, make forward progress, and recover from
   a temporary person crossing without discarding the global goal.
2. **Liveness/glass:** observe at least ten minutes near representative glass
   and furniture. Any unexpected stationary period over ten seconds fails the
   run. A repeated failed approach must create a spatial penalty and choose a
   different frontier instead of returning immediately.
3. **Sleep scan:** select Sleep Analysis. The dog must keep patrolling, then at
   a safe boundary stop, lower the rear/aim the camera up, scan for no more than
   the configured 3–9 seconds, restore neutral stance, and resume its route.
   Verify the physical pitch direction before allowing a person close.
4. **Manual priority:** take over during ordinary patrol, during a scan, while
   approaching, and while waiting for the form. Each takeover must stop the
   autonomous owner immediately. Test W/S, A/D turn, Q/E strafe, Shift fast
   gear, key release, browser blur, and lost network; velocity must clear
   within 500 ms. Autonomous policy must never retake control until selected.
5. **Consent path:** with a real face in range, verify one stable fatigue target
   leads to one approach/greeting and one active 180-second QR/NFC session.
   Test decline, consent without escort, consent with escort, and timeout.
   Only the signed active visitor response may move the robot.
6. **Escort:** confirm all saved rest areas persist for the venue. An accepted
   escort must choose a usable area, announce the route, arrive, say farewell,
   and return to Sleep Analysis. No second fatigue event may interrupt it.
7. **Map controls:** create and reload a focus area and keep-out zone, switch
   Full Venue → Focus Area, and restart the stack. The shapes must stay in the
   same map coordinates and keep-out hazards must remain active in both modes.
8. **Social regression:** wave and sustained gaze triggers must each have a
   logged cause and cooldown. A response must not cancel the current route or
   create repeated stationary waving.

Record timestamps, mode epochs, movement owner, hold reason/deadline, camera
age, chosen goal, interaction id, and result for every failed step. Do not tune
around a failure until its exact owner and state transition are identified.
