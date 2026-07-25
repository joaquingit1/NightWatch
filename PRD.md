# Night Watch — Product Requirements

Status: current product truth and implementation direction  
Platform: Unitree Go2 running DimensionalOS  
Primary principle: curiosity is the robot's base operating state, not an idle feature

## 1. Product

Night Watch is an autonomous robot dog that continuously explores and patrols a floor,
looks for consented people showing signs of fatigue, approaches them, explains the
concern without making a medical claim, and offers to guide them to a tagged rest area.

The robot should feel alive before a user asks it to do anything. Mapping, patrolling,
looking around, and observing are the background job. Night Watch tasks temporarily
preempt that job; when they finish, fail, or time out, curiosity resumes automatically.

This is not a stationary sleepiness kiosk with a robot attached. The product value is
the closed physical loop:

`explore → observe → assess → approach → offer help → escort → resume patrol`

## 2. Product laws

### 2.1 Curiosity is the default

- Curiosity starts as soon as sensors, localization, and a safe costmap are ready.
- Agent/LLM thinking does not stop curiosity. Cognition and base motion are concurrent.
- A command acquires a temporary behavior lease. Completion, failure, cancellation, or
  lease expiry returns control to curiosity within one second.
- Before the floor is mapped, curiosity means frontier exploration.
- After map completion, curiosity means coverage patrol weighted toward least-recently
  visited and high-value areas.
- A nearby person may cause a short, identity-locked curious follow, but it is time-boxed
  and never outranks a Night Watch intervention or an explicit user command.

### 2.2 Three-second liveness invariant

When autonomy is enabled and motion is safe, the robot must not be unintentionally
motionless for more than three seconds.

This is a supervisor invariant, not an instruction to drive into people. Safety always
wins. Remaining still for more than three seconds is valid only in an explicit hold
state with a machine-readable reason:

- user requested stay/stop;
- e-stop or manual control;
- low battery or charging;
- connection/localization/costmap unavailable;
- planner reports no safe motion;
- a person or obstacle is inside the configured safety envelope;
- an intervention intentionally requires the robot to wait.

If translation is unsafe but rotation is safe, curiosity may scan in place. If all
motion is unsafe, the robot publishes zero velocity and exposes `HOLD:<reason>` rather
than pretending it is still exploring.

### 2.3 One motion owner

Only one behavior may own autonomous motion at a time. Priority is:

1. e-stop and manual teleoperation;
2. safety and battery hold;
3. explicit stay/stop;
4. escort or explicit navigation command;
5. fatigue intervention approach;
6. short person follow/observation;
7. frontier exploration;
8. mapped-floor coverage patrol;
9. safe scan/gesture recovery.

The LLM does not arbitrate velocities. A deterministic supervisor grants renewable
leases and routes every autonomous command through DimensionalOS's movement manager.

## 3. Night Watch workflow

### 3.1 Observe concurrently

A lightweight person detector runs while the curiosity behavior owns motion. It emits
anonymous tracks. The sleep-deprivation model runs only when image quality and face
geometry are sufficient; it may sample or queue tracks to remain within the live
compute budget.

During `MAPPING`, detections may run in shadow mode for performance testing, but no
fatigue intervention or longitudinal identity is created. Reaching the operational
`MAPPED` gate enables Night Watch monitoring, anonymous labeling, and interventions.
Obstacle avoidance and a short safety/curious response to a nearby person remain active
in both phases.

The model integration contract is `FatigueAssessment`:

```text
assessment_id
timestamp
track_id
anonymous_person_id?       # only when re-identification is confident
bbox
fatigue_score              # model-defined normalized score
confidence
quality
factors                    # model explanation, not a diagnosis
observation_seconds
model_version
```

The model owner controls feature extraction and scoring. The robot policy controls
when an assessment is actionable. One frame is never sufficient for intervention.

### 3.2 Confirm and approach

An assessment becomes an intervention candidate only after:

- confidence and quality exceed agreed thresholds;
- the signal persists across a minimum observation window;
- the person is not in cooldown and has not declined recently;
- the planner can reach a respectful standoff position;
- no higher-priority behavior or safety hold is active.

The robot locks the target track, approaches to approximately 1–1.5 m, stops, and
delivers a short non-medical message such as: “You look tired. Would you like me to
guide you to the rest area?”

### 3.3 Response without relying on a microphone

Microphone input is not part of the MVP. The Go2 blueprint exposes no verified audio
input, and venue noise makes speech recognition a poor critical dependency.

The primary response surface is a static NFC tag or QR code on the robot. It opens a
mobile page for the current short-lived intervention:

- `Guide me to the rest area`
- `Remind me later`
- `No thanks`

The page submission creates a signed `EscortRequest`. A stale request or a request with
no active nearby target is rejected safely. The transport must be proven on the actual
venue network; a cloud relay and an operator-confirmed local fallback are both required
until networking is reliable.

Audio output is additive. `speak()` has worked in the current stack, but whether sound
comes from the robot or the laptop and whether it is audible in the venue must be
measured. The flow remains usable with screen/NFC only.

### 3.4 Escort and return

The rest area is a required setup tag and a saved-map location. On an accepted request:

1. bind the request to the currently locked nearby person;
2. acquire the escort motion lease;
3. announce/display that guidance is starting;
4. navigate to the tagged rest area at reduced speed;
5. arrive, record the outcome, and release the lease;
6. resume curiosity within one second.

The MVP guides a willing person who follows the robot. Confirming that the person
continues following is a later enhancement because the Go2 has no rear-facing camera.

## 4. Mapping lifecycle

### 4.1 Unmapped floor

Frontier exploration runs continuously; it is not divided into arbitrary idle bursts.
Failures trigger a different frontier or a bounded recovery scan. The supervisor uses
odometry and command heartbeats to detect “commands sent but robot not moving.”

### 4.2 Map-complete decision

“No frontier once” is not map completion. A floor becomes `MAPPED` only when all are
true:

- the explorer reports no reachable frontier for a configured number of attempts;
- this is stable from more than one robot pose;
- the costmap has exceeded a minimum explored-area threshold;
- critical locations, including `rest_area` and `home`, are tagged;
- the mapping database has been flushed and an export smoke test succeeds.

Map completeness is revocable when a new frontier appears.

### 4.3 Mapped floor

After mapping, curiosity becomes a coverage patrol. The patrol chooses among safe
waypoints using:

- time since last visit;
- expected people density;
- current intervention candidates;
- battery cost and distance to home;
- temporary blocked-zone penalties.

This transition also enables the Night Watch monitoring policy. The robot starts
creating anonymous person records and scheduling fatigue assessment during patrol; it
does not wait at a fixed observation station.

The saved `.pc2.lcm` premap must be integrated with DimensionalOS relocalization before
the product claims it remembers the floor across restarts. Saving a `.db` alone is not
enough.

## 5. Anonymous people and longitudinal presence

The MVP does not perform unrestricted named facial recognition.

It uses anonymous, local, session-scoped person re-identification based on tracker and
appearance embeddings. Raw face frames are not retained by default. An identity match
must carry a confidence score and may remain `unknown`.

Reliable labeling is opt-in:

- an NFC/QR user may provide an alias;
- the alias is associated with an anonymous embedding only with explicit consent;
- the person can decline, expire, or delete the association.

Presence accounting is conservative:

- a `presence_checkpoint` increments at most once when the same high-confidence person
  is seen at least two hours after their last credited checkpoint;
- “15 hours present” requires sightings across the interval, not merely a first and last
  timestamp;
- the late-night rule is eligible when the observed span is at least five hours and the
  latest sighting is after 03:00 local time;
- these rules raise intervention priority but never constitute a medical diagnosis.

Opt-in facial embeddings are a stretch goal only after the anonymous flow works and has
deletion, expiry, confidence gating, and false-match tests.

## 6. Current implementation truth

Live-verified before the current low-battery shutdown:

- live Go2 connection, manual control, A* navigation, obstacle-aware frontier goals;
- latest-only JPEG camera transport and MJPEG feed with measured source age below
  150 ms in normal operation (17.5 ms in the last check), with no stale-frame FIFO;
- live voxel map/costmap and throttled Rerun visualization;
- generic-person detection and a 25-second identity-locked follow of the same test
  person, including three successful reacquisitions;
- explicit command/follow motion routed through one DimensionalOS movement manager;
- persistent LiDAR/odometry recording and tested map reconstruction;
- semantic image memory persistence;
- immediate curiosity as the base state, including exploration while the agent thinks,
  time-boxed close-person following, and deterministic return to mapping;
- battery gate for self-initiated movement at 10% state of charge;
- MCP skills for movement, exploration, visible-object approach, speech, and tagging.

Implemented and unit-tested, awaiting a charged-robot field test:

- authoritative rolling-window map fusion. The Go2 WebRTC stream is a complete
  6.4 m rolling voxel snapshot, not a raw scan; each new window now replaces its
  overlapping map region so a departed person is removed instead of becoming a wall;
- corrected patrol goal handling: a cancelled/replanned goal is no longer mistaken for
  a successful arrival;
- coverage-aware exploration fixes: rolling information delta is no longer treated as
  map completion, frontier goals are recorded once, temporary failure regions are
  penalized, patrol visit history survives preemption, and Euclidean clearance retains
  viable diagonal/narrow corridors;
- macOS camera-process isolation: the common `Image` type no longer eagerly loads
  OpenCV's private AVFoundation/FFmpeg bundle into the PyAV WebRTC worker, removing
  the duplicate Objective-C-class condition that explicitly warned of camera crashes;
- bounded recovery and explicit safety/lease hold reasons in the curiosity supervisor;
- map-completion transition from frontier exploration to mapped-floor patrol;
- automatic lie-down on the low-battery safety hold;
- dedicated `/operator` command center with latest camera, behavior owner, battery,
  map phase, navigation state, measured camera age, direct agent text, and
  deterministic Hold, Resume, dog-expression, home, follow, and navigation controls;
- bounded, safe dog-like expression during stationary curiosity: hip wag, happy
  motion, paw scrape, brief sit, wave, and stretch/play bow. Expressions start before
  the three-second liveness deadline, yield to higher-priority motion, and return
  immediately to exploration or patrol. The fixed-body Go2 uses whole-body scan turns
  for looking around because it has neither a physical tail nor an actuated neck;
- saved-map relocalization wiring and a clean 5 cm premap export containing 89,238
  finite points over 13.3 m × 9.85 m. Runtime still needs to prove a stable accepted
  alignment before stored-location navigation is enabled.
- AprilTag anchors with two-sighting acceptance and safe standoff tags for `home`,
  `rest_area`, and `sleeping_area`;
- sparse DimensionalOS experience memory plus automatic camera-stale, stuck, loop, and
  repeated-goal analysis with feedback into frontier selection;
- promptable YOLO-E semantic detection, 3D object promotion after repeated evidence,
  autonomous major-area classification/tagging, and sleeping-space inference from
  tents, sleeping bags, cots, and beds;
- local anonymous full-body OSNet person galleries, ambiguity refusal, cross-session
  `person_id` selection, bounded storage, consent-gated aliases, and sustained
  identity checks during follow;
- operator status for semantic areas, stable objects, and remembered people.

Not built or not yet integrated:

- sleep-deprivation model integration;
- longitudinal two-hour presence-credit and 15-hour/late-night rules;
- NFC/QR response page and robot request channel;
- intervention policy and rest-area escort;
- verified robot-mounted speaker output and venue audibility;
- any verified microphone input.

## 7. MVP and cut line

The demo-critical MVP is:

1. curiosity supervisor with the three-second invariant;
2. continuous mapping followed by coverage patrol;
3. sleep model assessments displayed and consumed through the frozen contract;
4. one consented fatigue intervention;
5. NFC/QR acceptance;
6. navigation to a tagged rest area;
7. automatic return to curiosity;
8. live operator UI with state, hold reason, target, map phase, battery, and e-stop.

Longitudinal re-identification is secondary. Named facial recognition, breathing
monitoring, belongings protection, multi-nap scheduling, wake ladders, and a
leaderboard are excluded from the MVP unless the complete loop above is already
reliable.

## 8. Success criteria

- On a safe test course, 30 minutes with zero unexplained still periods over three
  seconds. Planner computation, obstacle safety, low battery, manual hold, and behavior
  preemption are explained holds and must never be bypassed merely to create motion.
- Every intentional hold reports a reason within 500 ms.
- Ten consecutive behavior preemptions return to curiosity within one second.
- Frontier exploration resumes after a failed/finished task without an LLM turn.
- A person staged in and then removed from the rolling LiDAR window disappears from the
  navigation costmap; revisiting the same corridor does not preserve a ghost wall.
- A cancelled patrol goal triggers replanning without a rapid publish/cancel loop.
- A mapped-floor replay transitions to coverage patrol rather than repeatedly calling
  frontier exploration.
- The same test person remains locked through a four-second occlusion without switching
  to a bystander.
- Five staged model events yield exactly five policy decisions with no intervention
  below confidence/quality thresholds.
- Five NFC requests produce the correct accept/remind/decline outcomes and no stale
  request moves the robot.
- Five accepted requests navigate to `rest_area`; manual override stops each run.
- Restart against a saved premap relocalizes before stored-location navigation is
  enabled.
- No raw biometric video is retained by the person ledger unless an explicit test
  consent flag is active.

## 9. Operational constraints

- The runtime autonomy cutoff is 10%. Start autonomous field tests above 20% and a demo
  above 60%; the robot must not be left standing while critically low.
- Manual e-stop and an operator remain present during venue operation.
- The system says “fatigue risk” or “you look tired,” never “you are medically sleep
  deprived.”
- All behaviors must run without microphone input.
- Heavy model training may use MatPool GPUs; live camera, navigation, tracking, and
  policy remain local because WAN latency is incompatible with physical control.
- macOS is the current development platform but DimensionalOS documents it as
  experimental; Ubuntu/CUDA is the preferred demo fallback.

## 10. Execution

The phase PRDs in `prds/` describe the product slices; this is the current
evidence-driven order:

1. Charge and run the physical stabilization gate: dynamic-person removal, patrol
   replanning, obstacle clearance, camera freshness, follow safety, manual preemption,
   low-battery lie-down, safe dog-expression firmware support, and the operator console.
2. Export the clean mapping recording with rolling-window replacement, load the
   resulting `.pc2.lcm` through DimensionalOS relocalization, and prove a restart can
   navigate to `home` and `rest_area`.
3. Freeze and integrate the fatigue-model event contract without granting the model
   motion authority. The deterministic policy layer decides whether an event is
   actionable.
4. Build the NFC/QR consent page and expiring request channel, then connect accepted
   requests to the existing navigation lease and tagged `rest_area`.
5. Add a front-camera-safe lead-and-check escort state machine bound to the accepted
   anonymous `person_id`, then run the complete detect → request → consent → escort →
   return-to-curiosity loop repeatedly.
6. Add conservative longitudinal presence accounting only after physical
   cross-person/cross-session identity tests pass; named facial recognition remains
   outside the default design.

`SLEEPINESS-PIPELINE-TDD.md` remains the model developer's technical design. Where it
conflicts with this PRD, the runtime contract and capability gates in this PRD win; any
model performance claim must be replaced with measured results.
