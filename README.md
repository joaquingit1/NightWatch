# 守夜犬 Night Watch

**每个 AI 都想让你更努力。它想让你休息。**  
_Every AI makes you work more. This one makes you stop._

An autonomous robot dog that patrols, spots when people are running on empty,
and escorts them to a recognized sleeping area. The current demo records the
nap and schedules a visible wake-check deadline; breathing verification and an
automatic wake ladder remain follow-on work rather than booth claims. Built
for [AdventureX 2026](https://adventurex.org) · Theme: **Reverse** ·
`#adventurex2026`

---

## The idea

Night Watch is an emotional support, autonomous dog. A Unitree Go2 robot dog roams wherever people push themselves too hard — a hackathon floor, an office at 2am, a study hall during finals — reads signs of fatigue from a camera, and offers rest instead of another caffeine hit. If you accept, it walks you to a nap zone, keeps watch over multiple sleepers at once, checks that belongings are untouched, verifies breathing without contact, and wakes you on schedule with a soft escalation: a whisper, then a paw wave, then a small celebration when you are back.

Everything runs locally on laptops over a private network. No cloud required for the live demo.

---

## What you will see

| Moment         | What happens                                                              |
| -------------- | ------------------------------------------------------------------------- |
| **Patrol**     | The dog moves through a mapped corridor on its own                        |
| **Triage**     | A live fatigue score (0-100) appears on the booth screen                  |
| **Diagnose**   | The dog pauses, takes a short reading, and explains what it sees          |
| **Prescribe**  | It recommends a nap in a warm, pre-recorded voice                         |
| **Escort**     | It leads you to the mattress at a careful pace                            |
| **Nap ledger** | Arrival registers the nap and starts a visible wake-check countdown       |
| **Expression** | A >60% hand cover queues a safe dog gesture without cancelling navigation |
| **Posture**    | Lie down holds indefinitely; Stand releases the hold and resumes safely   |

---

## How fatigue is detected

Night Watch does not guess from a single blink. It watches a rolling window of signals from your face and posture:

- **Eye openness** (PERCLOS, blink duration), corrected for head angle so looking sideways does not fake drowsiness
- **Head nods and yawns**
- **Slump** (neck-torso angle when body pose is visible)
- **Stillness and movement** over time

These roll up into a **RestScore** from 0 to 100, with a confidence indicator. Low confidence means the system holds back rather than calling you tired when the camera cannot see you clearly.

The planned sleeper-monitoring stage uses optical flow on the torso to estimate
breath rate and abstains when the signal is weak. It is not wired into the
current booth loop and must not be presented as a medical or safety device.

---

## System overview

```
                    ┌──────────────────────┐
                    │     Booth display     │
                    │  live feed · score   │
                    │  thought ticker      │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼────────┐ ┌─────▼─────┐ ┌────────▼────────┐
     │    Perception    │ │  Policy   │ │   Robot dog     │
     │  camera + face   │ │  care     │ │  navigate ·     │
     │  fatigue score   │ │  loop     │ │  speak · watch  │
     └────────┬────────┘ └─────┬─────┘ └─────────────────┘
              │                │
     ┌────────▼────────────────▼────────┐
     │           Rest Ledger             │
     │   naps · rounds · wakes · stats   │
     └───────────────────────────────────┘
```

**Perception** turns camera frames into a fatigue score. **Policy** runs a deterministic care loop (triage, approach, escort, rounds, wake). The **robot** executes movement and speech through [DimensionalOS](https://github.com/dimensionalOS/dimos) on a Unitree Go2. The **booth UI** shows what the system is thinking in real time. The **ledger** keeps an honest record of the night.

The Go2 operator workbench is served at `http://127.0.0.1:5555/operator`.
Its exploration/cruise modes, manual keyboard controls, fatigue interaction,
QR/NFC binding, and unique Bedroom behavior are specified in
[`prds/P11-operator-workbench-v2.zh-CN.md`](prds/P11-operator-workbench-v2.zh-CN.md).
The form URL defaults to local development and can later be replaced with
`NIGHTWATCH_PUBLIC_FORM_URL=https://...`.

---

## Technology

| Layer           | What we use                                                                  |
| --------------- | ---------------------------------------------------------------------------- |
| Robot           | Unitree Go2 via DimensionalOS (WebRTC)                                       |
| On-device AI    | Local LLM (Qwen via Ollama) for agent commands; no internet needed in demo   |
| Vision          | YOLOv8-Face detection + MediaPipe face landmarks, OpenCV                     |
| Fatigue scoring | Interpretable thresholds, with optional learned models trained on venue data |
| Breathing       | Planned optical-flow verification; not active in the current demo            |
| Voice           | Robot TTS fallback chain plus two reviewed booth WAV cues                    |
| Booth           | Live video streams, score card, thought ticker, leaderboard                  |
| Data            | Consent-gated capture sessions; SQLite ledger for the night's events         |

---

## Privacy

- Analysis consent is required before any session.
- Raw video is stored only if you opt in separately.
- People who do not adopt the dog are never identified or named on screen.
- The leaderboard uses aliases for volunteers who choose to appear.

---

## Built at AdventureX

Night Watch was built in 72 hours at China's largest hackathon, among thousands of people who had not slept enough to build a product about sleep. The demo is the product: a dog that patrols real nappers, records real wakes, and publishes real data from the venue.

If you are a judge, sponsor, or visitor at the booth: ask for the live loop, the ledger timeline, or the engineering wall. We are happy to walk through what we measured, what we trained, and what we chose not to claim.

---

## Development (client + server + fatigue detection service)

The booth stack is three independently run pieces:

| Service               | Path                                 | Port | Role                                                                               |
| --------------------- | ------------------------------------ | ---- | ---------------------------------------------------------------------------------- |
| **Booth UI**          | `client/` (Next.js)                  | 3000 | Live video, RestScore, thought ticker, ledger                                      |
| **Policy API**        | `server/` (FastAPI)                  | 8000 | Care loop, ledger, camera capture, proxies fatigue data to the UI                  |
| **Fatigue detection** | `fatigue_fastapi_service/` (FastAPI) | 8001 | Real YOLOv8-Face + MediaPipe perception (EAR/MAR/PERCLOS/head-pose) over WebSocket |

`server/` streams real frames to the fatigue service by default
(`SCORER_BACKEND=live`) and can fall back to a scripted score
(`SCORER_BACKEND=stub`) for offline development.

**Recommended:** develop on **native Windows** (`C:\Users\...\NightWatch`) for faster file I/O and simpler `pnpm`/`uv` tooling. Keep the repo on an NTFS path, not under `\\wsl$\...`.

**DimOS / Go2 robot work** still requires **WSL Ubuntu** when you integrate the real dog. The web UI and both FastAPI services run fine on Windows alone.

### Prerequisites (Windows)

- [Node.js 20+](https://nodejs.org/) with [pnpm](https://pnpm.io/) (`corepack enable`)
- [Python 3.12](https://www.python.org/) for `server/` (with [uv](https://docs.astral.sh/uv/) if available, or a plain `venv`)
- **Python 3.12** (not 3.13) for `fatigue_fastapi_service/` — MediaPipe does not ship 3.13 wheels yet

### Run locally (Windows PowerShell)

Terminal 0 — Insta360 bridge (port 5556), optional when using
`CAMERA_SOURCE=insta360`:

```powershell
# Requires the proprietary SDK in the git-ignored folder (not redistributable).
$env:INSTA360_SDK_ROOT = "C:\path\to\Windows_CameraSDK-2.1.1_MediaSDK-3.1.3"
cd insta360_bridge
.\build.ps1
.\build\Release\insta360_bridge.exe --port 5556
```

See [`insta360_bridge/README.md`](insta360_bridge/README.md) for camera setup.

Terminal 0b — RTMP ingest + HLS (ports 1935 / 8080), optional for the booth and
operator **RTMP** video toggle (and `CAMERA_SOURCE=rtmp`):

```powershell
# Starts nginx-rtmp from docker-compose.yml (uses nginx-rtmp.conf).
docker compose up -d

# Publish a stream (OBS server URL, or ffmpeg):
#   Server: rtmp://localhost:1935/live   Key: stream
ffmpeg -re -i input.mp4 -c:v libx264 -f flv rtmp://localhost:1935/live/stream
```

Play URL is `http://localhost:8080/hls/stream.m3u8`. The booth `VideoFeed` and
operator console expose it via the **RTMP** button; browser playback needs no
`CAMERA_SOURCE` change. To also feed the fatigue pipeline from RTMP, set
`CAMERA_SOURCE=rtmp` (and `RTMP_HLS_URL` if you changed the host/port).

Terminal 1 — fatigue detection service (port 8001), only needed for `SCORER_BACKEND=live`:

```powershell
cd fatigue_fastapi_service
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:FATIGUE_DEVICE = "cpu"   # or "auto"/"mps"/"cuda:0" if you have a GPU
cd ..
.\fatigue_fastapi_service\.venv\Scripts\python.exe -m uvicorn fatigue_fastapi_service.app.main:app --host 127.0.0.1 --port 8001 --workers 1
```

`--workers 1` is required: the engine only tracks one active detection stream per process. See `fatigue_fastapi_service/README.md` for CPU-only PyTorch install notes (avoids pulling a large CUDA wheel) and the full WebSocket protocol.

Terminal 2 — policy API (port 8000):

```powershell
cd server
uv sync   # or: python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env.local -ErrorAction SilentlyContinue
# edit .env.local and set SCORER_BACKEND=live to use Terminal 1's real detection
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 3 --env-file .env.local
```

Terminal 3 — booth UI (port 3000):

```powershell
cd client
Copy-Item .env.local.example .env.local -ErrorAction SilentlyContinue
pnpm install
pnpm dev
```

Open `http://localhost:3000`. API requests use the Next.js rewrite; the
long-lived MJPEG and SSE connections use
`NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` directly so they are not
buffered by the development proxy.

Public nap intake (QR flow) is at `http://localhost:3000/form`. Answers persist
in SQLite (`data/nightwatch.db` by default), and pending escorts appear on the
booth page.

### Verify everything

```powershell
Invoke-WebRequest http://127.0.0.1:8001/health -UseBasicParsing   # fatigue service (if running)
Invoke-WebRequest http://127.0.0.1:8000/api/score -UseBasicParsing
Invoke-WebRequest http://localhost:3000 -UseBasicParsing
```

### API endpoints (C5 contract)

| Endpoint                             | Method | Purpose                                             |
| ------------------------------------ | ------ | --------------------------------------------------- |
| `/video_feed/pov`                    | GET    | Raw camera MJPEG                                    |
| `/video_feed/annotated`              | GET    | Annotated fatigue overlay MJPEG                     |
| `/video_feed/robot`                  | GET    | Go2 first-person MJPEG proxy (`ROBOT_CAMERA_URL`)   |
| `/text_stream/thoughts`              | GET    | SSE stream of policy events                         |
| `/api/score`                         | GET    | Latest fatigue frame JSON                           |
| `/api/plan`                          | GET    | Scheduler route with ETAs                           |
| `/api/ledger`                        | GET    | Nap timeline                                        |
| `/api/leaderboard`                   | GET    | Fatigue leaderboard                                 |
| `/api/adopt`                         | POST   | Adoption form                                       |
| `/api/capture`                       | POST   | Capture session upload                              |
| `/api/outcome`                       | POST   | Post-wake survey                                    |
| `/api/robot/status`                  | GET    | Robot connectivity, behavior, map, and bridge state |
| `/api/form/schema`                   | GET    | Intake questionnaire + `session_id`                 |
| `/api/form/responses`                | POST   | Submit public nap intake                            |
| `/api/form/responses/latest`         | GET    | Latest intake rows for booth                        |
| `/api/form/responses/pending-escort` | GET    | Visitors waiting for escort                         |
| `/api/form/responses/{id}`           | PATCH  | Acknowledge, escort, or decline                     |

Stub implementations remain available with `DEMO_MODE=stub` and
`SCORER_BACKEND=stub`.

### Integrated Go2 mode

The repository now also contains the real DimensionalOS hardware package in
`nightwatch/`. In live mode the services form one loop:

```text
Go2 camera (:5555) -> policy API (:8000) -> fatigue model (:8001)
       ^                    |                       |
       |                    +-- booth UI (:3000) <--+
       +-- assessment POST + MCP intervention/escort calls (:9990)
```

The bridge does not act on one frame. It requires consecutive windows from the
same anonymous track, minimum confidence/quality, a minimum observation time,
and a per-person cooldown. It first calls `potential_detected`; only stronger
sustained evidence then calls `escort_to_sleeping_area`. Every assessment is
also shown on the robot operator page and written to a local JSONL audit trail.
The QR/NFC form asks the visitor's current energy level and whether they want
guidance. The first valid response bound to the live three-minute interaction
can dispatch the escort directly; successful robot completion marks the request
escorted.

After creating `server/.venv`, `fatigue_fastapi_service/.venv`, and installing
the client packages as described above, start all user-space services with:

```bash
./run_integrated.sh
```

Start the Go2 separately with `./nightwatch/run_scout.sh`, or have the launcher
start it too:

```bash
./run_integrated.sh --with-robot
```

The launcher always uses `SCORER_BACKEND=live` unless overridden. Without
flags it uses the local webcam and leaves robot calls disabled. With
`--with-robot`, it uses the Go2 camera, enables the assessment/action bridge,
and starts the scout stack.

Thresholds and endpoints are configurable in `server/.env.example`. The robot
can remain offline during frontend/model development; `/api/robot/status`
reports the disconnected state and no motion calls are attempted.

---

## License

TBD.
