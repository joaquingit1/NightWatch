# 守夜犬 Night Watch

**每个 AI 都想让你更努力。它想让你休息。**
_Every AI makes you work more. This one makes you stop._

Night Watch is an autonomous Unitree Go2 that patrols a venue, watches faces for
signs of fatigue, invites tired people to rest through a QR/NFC questionnaire,
escorts whoever accepts to a marked sleeping area, and logs the nap.

Built in 72 hours for [AdventureX 2026](https://adventurex.org) · Theme:
**Reverse** · `#adventurex2026`

---

## The loop / 运行闭环

1. **Patrol.** The dog explores the venue on its own with A\* navigation over a
   saved venue map, respecting operator-drawn focus and keep-out areas.
2. **Analyse.** A YOLOv8-Face + MediaPipe pipeline scores fatigue (EAR, MAR,
   PERCLOS, head pose) on every frame. In Sleep Analysis mode the dog can also
   stop at a safe boundary, lower its rear to aim the camera upward, and run a
   bounded 3-9 second sleep scan before resuming its route.
3. **Invite.** Sustained evidence from one anonymous track (minimum confidence,
   quality, observation time, consecutive windows, per-person cooldown) or a
   single operator press of **START INTAKE** makes the dog approach, speak an
   invitation, and open a signed, expiring QR/NFC intake session.
4. **Consent.** The visitor scans the code and answers the questionnaire on
   their own phone. Nothing moves the robot until a submission is bound to that
   active session.
5. **Escort.** `escort_to_sleeping_area` takes an uninterruptible priority-60
   behaviour lease, walks the person to the nearest known sleeping area,
   announces arrival, and releases.
6. **Log.** The arrival is written to the rest ledger and shown on the booth
   screen with a wake-check countdown.

---

## Architecture and ports / 架构与端口

| Process | Path | Port | Role |
| --- | --- | --- | --- |
| Booth client (Next.js) | `client/` | 3000 | `/` booth wall, `/lidar` map operator surface, `/first-person` full-screen robot camera, `/form` intake questionnaire |
| Policy API (FastAPI) | `server/` | 8000 | Camera capture, live scoring, rest ledger, zones, intake sessions, robot bridge |
| Fatigue model (FastAPI) | `fatigue_fastapi_service/` | 8001 | YOLOv8-Face + MediaPipe detection over WebSocket (`/v1/streams/detect`) |
| Robot process (DimensionalOS) | `nightwatch/` | 5555 | `dimos run nightwatch.scout`; operator console at `/operator`, camera at `/video_feed/camera` |
| MCP skill server | inside the robot process | 9990 | `POST /mcp`; how the policy server calls `tag_area_here`, `escort_to_sleeping_area`, mode changes, speech |
| Map streamer | inside the robot process | 8010 | `ws://127.0.0.1:8010/ws/map` point cloud + odometry, relayed by the server as `/ws/lidar` |
| Insta360 bridge (optional) | `insta360_bridge/` | 5556 | MJPEG from the 360 camera when `CAMERA_SOURCE=insta360` |

```text
Go2 camera (:5555) -> policy API (:8000) -> fatigue model (:8001)
       ^                    |                       |
       |                    +-- booth UI (:3000) <--+
       +-- MCP skills (:9990)          ^
                                       |
   map streamer (:8010) --- /ws/lidar -+

phone -> Tencent Cloud host (nginx, public /form)
             |  reverse SSH tunnel, remote loopback :18020
             +-> booth policy API (:8000) /api/form/*
```

The cloud host serves the public questionnaire page and proxies
`/api/form/schema` and `/api/form/responses` back to the booth machine through a
reverse SSH tunnel. Schema and submission both have to reach the booth process:
the active interaction, the signed token, and the pending robot response only
exist there, so a cloud-only form could never bind a visitor to the dog standing
in front of them.

---

## Running it / 启动

Booth only (webcam, no robot calls):

```sh
./run_integrated.sh
```

Full stack with the dog:

```sh
./run_integrated.sh --with-robot --camera robot --venue legacy
```

`--with-robot` switches the camera to the Go2, enables the assessment/action
bridge, and starts `nightwatch/run_scout.sh`. `--venue NAME` selects the venue
bundle (map, zones, home/breadcrumbs, semantic memory); `legacy` is the default
profile and holds the existing surveyed map. On Ctrl-C the launcher exports the
session map back into the saved premap.

Prerequisites, created once:

```sh
cd client && pnpm install
cd server && uv sync                 # or python -m venv .venv + requirements.txt
cd fatigue_fastapi_service && python3.12 -m venv .venv && \
  .venv/bin/pip install -r requirements.txt   # MediaPipe has no 3.13 wheels
```

**The Mac must be joined to the robot's Wi-Fi to control the dog.** Without the
robot, `/lidar` is empty because nothing publishes on `:8010`. Serve the saved
premap instead so the venue still renders:

```sh
dimos/.venv/bin/python scripts/serve_saved_map.py
```

It speaks the same wire protocol as the real `MapStreamer` and replays the last
accepted `world -> map` alignment, so operator zones land where the robot last
had them. `run_scout.sh` stops it automatically before the hardware streamer
starts. `scripts/fake_map_stream.py` is the synthetic equivalent for UI work.

---

## Marking a sleeping area / 标记休息区

There are four ways to declare where people may nap, and they all converge on a
single `areas` row in the robot's world model, which is the only thing
`escort_to_sleeping_area` reads:

1. **Robot self-tagging.** During patrol the world model votes on what it is
   looking at and promotes a confident area to `sleeping_area` on its own.
2. **`tag_area_here`.** The **MARK SLEEP AREA HERE** button on `/lidar` calls
   this skill through MCP with `{"name": "sleeping_area"}`, tagging the dog's
   current pose.
3. **A `sleeping` polygon drawn on `/lidar`.** Unlike `keep_in` / `keep_out`,
   this is not a movement constraint: the robot side turns the polygon's
   centroid into a navigable area.
4. **AprilTag id 2.** Physical markers are stable manual anchors (0 =
   checkpoint, 1 = rest, 2 = sleep). Seeing tag 2 writes the sleeping area at a
   safe approach pose rather than at the marker itself.

Human-authored areas (2, 3, 4) are marked `protected`. Autonomous vote
resolution keeps accumulating evidence but never overwrites an operator's
decision. Multiple sleeping areas are supported on purpose: the escort picks the
nearest reachable one by path cost.

---

## Operator console / 操作台

`http://localhost:5555/operator`, bilingual, designed to be usable without
scrolling on the booth screen.

- **Operating mode**, one authoritative state machine on the robot side:
  **Autonomous** (free exploration), **Sleep Analysis** (patrol plus fatigue
  scans and the escalation policy), **Manual Override** (`M`). Manual is
  latched and immediate; only safety may override it, and velocity is cleared
  within 500 ms on key release, browser blur, or disconnect.
- **Emergency stop**, always visible, bound to `Space`.
- **Manual driving**: `W`/`S` forward and back, `A`/`D` turn, `Q`/`E` strafe,
  with a fast gear.
- **Voice panel**: six preset lines (greeting, invite to rest, prescribe a nap,
  escort start, arrival, farewell) plus a free-text field that speaks anything
  typed.
- **START INTAKE**: speaks the invitation and opens a QR intake session bound to
  the current interaction.
- **AUTO ESCORT**: operator consent switch, **off by default**. While it is on,
  an incoming questionnaire submission dispatches the escort without a second
  press. It lives on the server, so it stays settable while the robot is
  offline, and Manual Override cancels an armed auto-escort in flight.
- Posture and expression actions (stand, lie down and hold, sit, wave, play bow,
  wiggle, paw scrape), `scan_now` for an immediate look-up scan, approach
  nearest person, stop follow, stop navigation, set/return home.
- Live camera with analysis overlay, behaviour owner, battery, map phase,
  navigation state, camera age, semantic areas, and remembered people.

The booth page at `:3000` mirrors the operator-relevant subset: mode, map phase,
hold reason, AUTO ESCORT, SHOW QR, lie down and stand.

---

## Configuration

`server/.env.example` documents every server knob: camera source, scorer
backend, CORS (loopback plus RFC1918 only), robot URLs, escalation thresholds
(`ROBOT_INTERVENE_SCORE`, `ROBOT_ESCORT_SCORE`, minimum confidence, quality,
observation seconds, consecutive windows, per-person cooldown), the LiDAR relay
upstream, the intake database path, and `INTAKE_SIGNING_SECRET`, which must be
the same private value on the booth and on any form proxy.

`nightwatch/run_scout.sh` sets the venue-scoped `NIGHTWATCH_*` paths for the
map, zones, keep-out, home, breadcrumbs, relocalization state, and semantic
memory. `NIGHTWATCH_PREMAP=off` forces live-map-only navigation.

The robot can stay offline for frontend and model work: `/api/robot/status`
reports the disconnected state and no motion calls are attempted.

---

## Testing / 测试

```sh
cd server && uv run pytest tests -q
cd nightwatch && ../dimos/.venv/bin/python -m pytest tests -q
cd client && pnpm exec tsc --noEmit
```

- `server/tests/` covers the zone store, intake QR binding, form binding, CORS
  config, the robot bridge, live scorer states, ledger policy, and web
  performance paths.
- `nightwatch/tests/` covers mode control, manual operator arbitration, and the
  behaviour regression suite. It needs the `dimos` virtualenv, not
  `nightwatch/.venv`.
- `fatigue_fastapi_service/tests/` holds the offline verification test.
- `nightwatch/HARDWARE-VALIDATION.md` is the supervised acceptance sequence that
  automated tests cannot replace. Run it only with a person on the physical
  emergency stop.

---

## Known limitations / operational gotchas

- **One Wi-Fi card, two networks.** Controlling the dog means joining the
  robot's Wi-Fi, which takes the Mac off the internet and drops the reverse SSH
  tunnel to the cloud questionnaire. Robot control and the public form are
  effectively mutually exclusive on a single laptop.
- **Battery floor.** Go2 firmware refuses motion below roughly 10% state of
  charge. Start field runs above 20%; at 5% the stack stops exploring and
  retraces its breadcrumb route home.
- **TTS latency.** The speech chain is kokoro to edge-tts to the local `say`
  command. Kokoro sounds best but synthesis is slow on a loaded machine, so the
  booth also ships two reviewed WAV cues and `say` remains the reliable
  fallback.
- **The ledger is in-memory.** `server/app/services/ledger_memory.py` resets on
  every server restart. Only intake responses are persisted, in SQLite at
  `data/nightwatch.db`.
- **QR and NFC point at the cloud.** The stickers encode an ordinary HTTPS URL
  ending in `/form` on the Tencent host (no Web NFC API involved), so they only
  work while that host is up and the tunnel is connected. Without the tunnel the
  booth still works at `http://localhost:3000/form`.
- **Maps are venue-scoped and explicit.** Venue selection is a launch flag, not
  a guess. Two visually similar corridors loading the wrong map is an unsafe
  failure mode.
- **macOS is experimental** for DimensionalOS Go2 support. Keep Go2 LAN latency
  under 10 ms with no packet loss and do not run a second camera client outside
  the stack.

---

## Repository layout

```text
client/                  Next.js booth UI, lidar viewer, intake form
server/                  FastAPI policy API, robot bridge, zone/intake stores
fatigue_fastapi_service/ YOLOv8-Face + MediaPipe fatigue model service
nightwatch/              DimensionalOS Go2 package (scout, escort, world model,
                         operator console) and its tests
insta360_bridge/         Optional C++ MJPEG bridge for the Insta360 camera
scripts/                 Offline map server, fake map stream, form-sync tunnel
deploy/tencent/          Nginx config and notes for the public form host
prds/                    Phase PRDs
```

Design documents worth reading before changing behaviour: `AGENTS.md` (repo
invariants), `nightwatch/LESSONS-2026-07-24.md`, `nightwatch/BRIDGE.md` (MCP
contract), `nightwatch/README.md`, `PRD.md`, and
`SLEEPINESS-PIPELINE-TDD.md`.

---

## Privacy

- Analysis consent is required before any recorded session.
- Raw video is stored only with a separate opt-in.
- People who do not accept are never identified or named on screen.
- The public questionnaire only reaches the robot through a signed, expiring,
  session-bound token.

---

## License

TBD.
