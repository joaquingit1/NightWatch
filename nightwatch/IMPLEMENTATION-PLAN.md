# Nightwatch capabilities 1–4: implementation and validation plan

Status: implemented in the DimensionalOS blueprint and unit-tested; physical
validation is intentionally pending a charged, supervised robot run.

## Why curiosity was circling

The July 23 logs show genuine outward frontier goals followed by repeated local
recovery. This was not evidence that the dog never moved. It was a state-loss
problem:

1. The stock explorer treated low information delta between two rolling
   costmaps as map completion. A rolling window can lose known cells when a
   temporary obstacle clears, so this signal is invalid.
2. The stock explorer wrote every attempted frontier into history twice.
3. Coverage patrol erased its visit map every time a person follow, operator
   command, or expression preempted it.
4. Coverage used square erosion for the Go2 footprint, rejecting diagonal and
   narrow passages that a circular footprint can safely traverse.
5. Firmware dog expressions stopped and restarted coverage, magnifying the
   history-reset problem.

`nightwatch.navigation` now makes completion depend on sustained
`NO_FRONTIERS`, records each goal once, strongly rewards novel regions,
temporarily blacklists failure regions, retains patrol visits across
preemption, and uses Euclidean clearance. Map completion is revocable when
coverage has no reachable goal. Automatic body expressions no longer interrupt
an active planner; bounded scan recovery supplies motion when the planner is
genuinely idle.

## 1. Marker anchors

Implementation:

- `MarkerDetectionStreamModule` detects AprilTag 36h11 markers from the Go2
  camera with quality gating.
- `MarkerTfModule` publishes marker poses into DimensionalOS TF.
- The world model requires two sightings before accepting a marker.
- Default IDs are `0=home`, `1=rest_area`, and `2=sleeping_area`.
- Navigation tags use a safe approach pose on the observed side of the marker,
  not the marker surface, so a wall-mounted marker does not become a collision
  goal.
- `NIGHTWATCH_MARKER_SIZE_M` configures the printed edge length; default
  `0.10`.

Validation gate:

- Print known-size tags, observe each twice, verify marker TF in Rerun, then
  execute five supervised navigations to each tag with manual override ready.

## 2. Experience memory and learning from mistakes

Implementation:

- Sparse, sharp, distance/time-gated JPEG keyframes and structured events are
  stored in DimensionalOS `memory2` at
  `assets/output/memory/nightwatch_experience.db`.
- The watchdog detects stale camera input, expected-motion stalls, repeated
  goals, and paths that travel far but return to the same region.
- A failure stores its pose, explanation, recommended correction, and a forced
  visual keyframe.
- Stuck/loop/repeated-goal regions are sent back to the explorer as temporary
  penalties, so the immediate retry chooses a more novel frontier.
- `analyze_recent_failures`, `world_status`, and
  `search_visual_memory` expose diagnostics to the robot agent.

This is policy adaptation, not online weight training. Safety-critical
locomotion remains deterministic and inspectable. Recorded runs can later be
replayed for tuning or model training without learning unsafe velocity actions
on the physical robot.

Validation gate:

- Stage one blocked frontier and one circular route. Verify one warning event,
  one keyframe, one temporary failure zone, and a different subsequent goal.
  Replay the database offline and confirm camera timestamps remain sparse.

## 3. Semantic 3D objects and autonomous area tags

Implementation:

- Promptable YOLO-E looks for high-value objects such as tents, sleeping bags,
  cots, beds, desks, kitchen equipment, exits, first-aid kits, extinguishers,
  and charging stations.
- Detections are lifted into the world frame with the synchronized Go2
  pointcloud and camera calibration.
- People are explicitly excluded from persistent static objects.
- Spatially consistent sightings are merged; an object becomes stable only
  after three observations.
- Robot poses are clustered into major areas. Repeated object evidence votes
  for a purpose (`sleeping_area`, `workspace`, `kitchen`, `lounge`, and so on);
  the existing shared Moondream model supplies a low-rate secondary vote for
  new or uncertain areas.
- A sleeping tent/bag/cot/bed is weighted evidence for a sleeping area.
- Stable classifications become DimensionalOS named locations. Operators can
  override or add a tag with `tag_area_here`.
- The persistent relational catalog lives at
  `assets/output/memory/nightwatch_world.sqlite3`.

Validation gate:

- Traverse one workspace and one staged sleeping space three times. Confirm
  stable object positions, the inferred labels, and safe navigation to the
  resulting area tags. A moving person must never appear in the object table.

Performance rule:

- Semantic inference is latest-only, runs in a dedicated worker, and is
  distance/time sampled. If physical testing shows camera age or planner
  cadence regression, increase `semantic_interval_s` or move only this
  asynchronous perception job to a nearby GPU service. WAN GPUs must never
  enter the live velocity-control path.

## 4. Anonymous same-person memory

Implementation:

- Full-body OSNet embeddings from DimensionalOS `TorchReIDModel` are stored
  locally in `assets/output/memory/person_identity.sqlite3`.
- No face recognition is used and no person crop is retained.
- Galleries are bounded to eight diverse views per anonymous identity.
- Matching requires both an absolute cosine threshold and a margin over the
  second-best identity; ambiguous views are refused.
- The existing BoT-SORT identity plus torso appearance handles fast,
  frame-to-frame following. OSNet adds cross-session selection and checks for
  a sustained tracker jump.
- Passers-by are catalogued sparsely while follow is inactive.
- `follow_person(person_id=...)` follows only the requested remembered person.
  `remember_visible_person`, `list_remembered_people`,
  `person_memory_status`, and consent-gated aliases are agent skills.
- Curiosity-initiated follows remain capped at 25 seconds. Explicit follow or
  escort ownership is not cut short by that curiosity timer.

Physical limitation:

- A front-camera Go2 can reliably follow a person in front. It cannot verify a
  person walking behind it continuously while simultaneously leading them.
  The escort MVP therefore needs a lead-and-check behavior (short waypoint,
  turn/check identity, continue) or has the person walk slightly ahead. It
  must not claim continuous rearward verification without a rear camera.

Validation gate:

- Test two similarly dressed people crossing, four-second occlusion, tracker-ID
  reset, departure/re-entry, and process restart. Any ambiguous match must stop
  rather than switch. Then test five explicit `person_id` follows.

## Command center

The command center is live at `http://localhost:5555/operator` when the stack
runs. It already sends text directly to the DimensionalOS robot agent and has
deterministic buttons for hold/resume, home, navigation stop, follow stop, and
safe dog expressions. Its status now also reports semantic areas, stable
objects, and remembered people. Rerun remains the 3D pointcloud/map view.

## Safe rollout order

1. Start above 20% battery with the robot lifted or in a clear pen. Check
   camera age, module startup, and manual override before autonomous motion.
2. Validate frontier novelty and retained patrol history without people.
3. Validate dynamic-person clearing and staged failure feedback.
4. Validate markers and semantic tags without navigating to them.
5. Navigate to each accepted tag under supervision.
6. Validate anonymous identity in place, then slow following.
7. Run a 30-minute mixed preemption test and inspect the experience report.

