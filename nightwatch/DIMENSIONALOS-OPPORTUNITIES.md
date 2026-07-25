# DimensionalOS opportunities for Nightwatch

This is the evidence-based DimensionalOS capability audit for Nightwatch.
Capabilities 1–4 are now implemented in the scout blueprint and unit-tested;
they still require the staged physical gates in
[the implementation plan](IMPLEMENTATION-PLAN.md).

## Implemented, awaiting physical validation

### 1. AprilTag or ArUco location anchors

Nightwatch now detects visual markers at `home`, `rest_area`, and
`sleeping_area`.
DimensionalOS marker modules publish marker poses into the transform tree, so
they can provide:

- a physical sanity check for saved-map relocalization;
- repeatable, easy-to-demo destinations;
- a robust rest-area target even when semantic image matching is ambiguous;
- a recovery anchor after the map drifts.

Two sightings are required and a collision-safe approach pose is tagged rather
than the marker surface.

### 2. Sparse searchable camera memory and offline diagnostics

Nightwatch now records quality-filtered, distance/time-gated camera keyframes
and structured failure events into `memory2`. It uses replay and the query DSL
to produce:

- camera/search evidence for the demo;
- speed, stuck, replan, and camera-freshness heatmaps over the floor;
- clips surrounding ghost-obstacle or follow failures;
- repeatable regression input without keeping the physical robot running.

This is a better route to learning from navigation mistakes than training an
end-to-end walking model. The deterministic navigation stack remains in
control; recorded failures improve costmap tuning, waypoint choice, and test
coverage.

### 3. Open-vocabulary 2D/3D detection and ObjectDB

Nightwatch now uses DimensionalOS promptable YOLO-E, 3D unprojection, and its
own confidence-gated persistent object/area catalog to distinguish:

- stable objects worth remembering, such as doors, tables, rest signs, and
  charging stations;
- people and movable objects that belong only in the live obstacle layer;
- named navigation targets discovered during exploration.

Promotion requires repeated space-time evidence. Human detections are never
written into the static terrain or persistent object catalog.

### 4. Appearance-based anonymous re-identification

`WorldBelief` contains DINOv2 appearance galleries, CLIP recall, and
present/absent/occluded reasoning. It is currently packaged around another
robot blueprint, so Nightwatch should adapt its concepts and modules rather
than assuming drop-in Go2 support.

Nightwatch now adapts this pattern with local full-body OSNet galleries,
confidence plus margin gates, bounded storage, and refusal on ambiguity. It
strengthens same-person following without named facial recognition. Physical
false-match and cross-session tests remain mandatory.

### 5. Hosted dimTELE

The current `/operator` center is local and purpose-built. DimensionalOS also
offers hosted browser/phone/VR teleoperation. Add hosted dimTELE when control is
needed off the robot LAN, while keeping:

- the local deterministic Hold/e-stop path;
- local WASD as the lowest-latency fallback;
- agent text and Nightwatch-specific status in `/operator`.

Remote WAN control should never be the only emergency stop.

## Evaluate after the MVP loop works

### TemporalMemory

The experimental Go2 temporal-memory blueprint turns video into an
entity/event graph. It could answer questions such as “who remained in this
area?” or “what happened before the robot got stuck?” Its interfaces are still
marked unstable, so it should consume recorded/replayed data first and stay
outside motion control.

### Simulation and replay

Use DimensionalOS replay and MuJoCo/DimSim scenarios for behavior arbitration,
timeouts, map transitions, and operator-action regression tests. Simulation is
valuable for policy state machines, but collision clearance and Unitree sport
routines still require supervised physical tests.

### Stream quality and temporal alignment

DimensionalOS quality filters and timestamp alignment are useful when the
fatigue model arrives. The runtime should align a fresh image, person track,
pose, and model result before intervention; stale or blurry samples should be
discarded rather than queued.

## Hardware/platform gated

DimensionalOS contains ray-cast mapping, MLS/HTC terrain mapping, CMU terrain
analysis, TARE exploration, FAR planning, and other 3D navigation blueprints.
They are compelling for stairs, rough ground, and more complete free-space
reasoning, but they expect raw or richer 3D LiDAR such as a Mid-360 and are
mainly a Linux/native path.

The current Go2 WebRTC LiDAR feed is a complete rolling 6.4 m voxel snapshot,
not a raw scan. Treating that snapshot as individual rays corrupts clearing
semantics—the exact class of error that created permanent people-shaped walls.
Do not enable a ray tracer or train an end-to-end walking policy on this input.
Revisit 3D planning after adding a raw-scan sensor and moving the demo runtime
to Ubuntu/CUDA.

## Recommended order

1. Physically validate the existing dynamic-obstacle clearing, dog expressions,
   manual preemption, saved-map alignment, follow, and camera freshness.
2. Physically validate marker anchors, sparse experience recording, semantic
   area inference, and anonymous identity.
3. Integrate the fatigue event contract, NFC consent page, and escort workflow.
4. Add the front-camera-safe lead-and-check escort state machine.
5. Consider hosted dimTELE and experimental temporal/3D modules after the
   reliable MVP loop exists.

## Primary references

- [Perception](https://dimensionalos.mintlify.site/capabilities/perception)
- [Memory](https://dimensionalos.mintlify.site/capabilities/memory)
- [Offline analysis](https://dimensionalos.mintlify.site/capabilities/memory/offline-analysis)
- [Hosted teleoperation](https://dimensionalos.mintlify.site/capabilities/teleoperation/hosted)
- [Navigation deep dive](https://dimensionalos.mintlify.site/capabilities/navigation/deep_dive)
- [Skills and MCP](https://dimensionalos.mintlify.site/agents/skills-and-mcp)
- [Unitree Go2 sport client commands](https://github.com/unitreerobotics/unitree_sdk2/blob/main/include/unitree/robot/go2/sport/sport_client.hpp)
