# Nightwatch Go2 scout

Nightwatch is the hackathon stack for a Unitree Go2 running on
[DimensionalOS](https://dimensionalos.mintlify.site). It explores map
frontiers, remembers semantic views, briefly follows a nearby person while
keeping the same track, and always gives manual/user work priority.

## Run

Join the robot's local network and start above 20% battery for field testing.
At 5% the dog stops exploration and retraces its persisted breadcrumb route to
the saved home position, then lies down:

```sh
./nightwatch/run_scout.sh
```

The launcher checks that the Go2 is reachable, activates the correct
DimensionalOS environment, starts the stack, and opens `dimos-viewer`.

- Command and control center: <http://localhost:5555/operator>
- Basic chat page: <http://localhost:5555>
- Direct latest-only camera: <http://localhost:5555/video_feed/camera>
- Backup keyboard e-stop/teleop: `PYTHONPATH=nightwatch dimos/.venv/bin/python -m nightwatch.teleop`

Curiosity is the immediate base state: the robot explores whenever no higher
priority behavior owns movement, including while the AI is thinking. A nearby
person can interrupt it for at most 25 seconds of identity-locked following;
mapping then resumes. Manual teleop, the operator Hold button, and explicit
agent commands outrank both above the critical threshold.

Text entered at `/operator` goes directly to the onboard DimensionalOS agent.
The page shows the latest camera plus behavior owner, battery, map phase,
navigation state, measured camera age, semantic areas, stable objects, and
remembered people. Action buttons bypass the language
model for low-latency Hold, Resume, Wave, Tail wag, Play bow, Paw scrape, Sit,
Happy wiggle, Lie down, Stand, Set home, Go home, Stop follow, and Stop
navigation commands. Lie down is a persistent posture hold: no autonomous
behavior resumes until Stand is pressed.
The native `dimos-viewer` remains the 3D point-cloud/map surface; it is launched
with the stack rather than embedded in this lightweight browser center.

## Dog-like expression

Firmware expressions run only when no navigation planner owns motion; they
cannot reset frontier or patrol history. If an active curiosity planner is
genuinely idle, the three-second liveness supervisor performs a bounded scan
and selects another goal. Safe explicit expressions favor `WiggleHips` (the
closest Go2 equivalent to wagging a tail), then happy/content motion, paw
scraping, a brief sit, wave, or stretch/play bow.

The Go2 has no physical tail or actuated neck. Looking around therefore uses
the existing small whole-body yaw scan, not a fictitious head joint. Flips,
jumps, dances, handstands, and wallowing are intentionally excluded from the
safe expression skill. Any explicit navigation, follow, safety hold, or
higher-priority behavior interrupts an expression with `StopMove`.

Covering more than 60% of the camera with a hand for three sampled frames
queues one safe expression. The queue waits for the active navigation leg to
finish naturally, so it never cancels a path or clears frontier history.

See [DimensionalOS opportunities](DIMENSIONALOS-OPPORTUNITIES.md) for the
ranked platform-capability audit and integration order.

## Saved maps and memory

Each run records throttled LiDAR snapshots plus odometry to:

```text
dimos/assets/output/maps/nightwatch_map.db
```

An existing recording is backed up rather than overwritten. Export a stopped
run to a reusable PointCloud2 map and Rerun recording with:

```sh
./nightwatch/export_map.sh
```

Semantic camera memories persist under:

```text
dimos/assets/output/memory/spatial_memory/
```

Sparse experience, semantic objects/areas, and anonymous person galleries use:

```text
dimos/assets/output/memory/nightwatch_experience.db
dimos/assets/output/memory/nightwatch_world.sqlite3
dimos/assets/output/memory/person_identity.sqlite3
```

See [capabilities 1–4 implementation and validation](IMPLEMENTATION-PLAN.md)
for marker IDs, self-tagging evidence gates, failure adaptation, person-memory
privacy rules, and the safe rollout sequence.

The exporter uses rolling-window replacement. The Go2 WebRTC LiDAR topic is a
complete 6.4 m voxel snapshot, not a raw scan; replacing the current window
allows people and other temporary objects to disappear from rebuilt maps
instead of becoming permanent walls. The exported `.pc2.lcm` is the input for
DimensionalOS's relocalization blueprint when revisiting an area; the `.db`
remains replayable for rebuilding at a different voxel size.

Premap use stays explicit until its physical alignment is field-verified:

```sh
NIGHTWATCH_PREMAP=auto ./nightwatch/run_scout.sh
```

This selects `assets/output/maps/export/nightwatch_map.pc2.lcm`.
Relocalization falls back to the live map until DimensionalOS accepts an
alignment, then merges the static premap with current local obstacles. Confirm
a stable `world → map` transform in Rerun before trusting stored-location
navigation. Set `NIGHTWATCH_PREMAP` to an absolute path to select another map;
set it to `off` for live-map-only navigation. The launcher uses the canonical
premap by default, and the exporter promotes a new canonical map based on its
actual XY floor footprint rather than file size.

## Verification

```sh
cd dimos
PYTHONPATH=../nightwatch .venv/bin/pytest -q ../nightwatch/tests
.venv/bin/ruff check ../nightwatch/nightwatch ../nightwatch/tests
```

The live camera path intentionally has no FIFO history: every consumer gets the
newest frame and superseded frames are dropped. Color images use bounded
JPEG-in-LCM payloads rather than raw shared-memory frames. The expanded Go2
voxel stream is sampled at the connection boundary so mapping cannot build a
backlog that starves the shared WebRTC camera worker.

## Known platform boundary

DimensionalOS marks macOS Go2 support experimental and recommends Ubuntu. This
stack avoids Apple MPS for YOLO, pins PyAV to the FFmpeg ABI used by OpenCV,
and lazily loads OpenCV so the WebRTC camera worker does not load two private
AVFoundation/FFmpeg implementations into one macOS process. It also includes
WebRTC camera recovery. For a production demo, keep Go2 LAN latency below
10 ms with 0% packet loss and avoid running another viewer or camera client
outside this stack.
