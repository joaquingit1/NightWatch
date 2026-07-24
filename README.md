# 守夜犬 Night Watch

**每个 AI 都想让你更努力。它想让你休息。**  
_Every AI makes you work more. This one makes you stop._

An autonomous robot dog that patrols, spots when people are running on empty, escorts them to nap, guards their sleep like a night nurse, and wakes them gently when it is time. Built for [AdventureX 2026](https://adventurex.org) · Theme: **Reverse** · `#adventurex2026`

---

## The idea

Hackathons celebrate staying up. Night Watch does the opposite. A Unitree Go2 robot dog roams the venue, reads signs of fatigue from a camera, and offers rest instead of another caffeine hit. If you accept, it walks you to a nap zone, keeps watch over multiple sleepers at once, checks that belongings are untouched, verifies breathing without contact, and wakes you on schedule with a soft escalation: a whisper, then a paw wave, then a small celebration when you are back.

Everything runs locally on laptops over a private network. No cloud required for the live demo.

---

## What you will see

| Moment        | What happens                                                             |
| ------------- | ------------------------------------------------------------------------ |
| **Patrol**    | The dog moves through a mapped corridor on its own                       |
| **Triage**    | A live fatigue score (0-100) appears on the booth screen                 |
| **Diagnose**  | The dog pauses, takes a short reading, and explains what it sees         |
| **Prescribe** | It recommends a nap in a warm, pre-recorded voice                        |
| **Escort**    | It leads you to the mattress at a careful pace                           |
| **Rounds**    | While people sleep, it returns on a schedule to check bags and breathing |
| **Wake**      | Ring, whisper, hello wave: never physical contact                        |
| **Ledger**    | Every nap, pass, and wake is recorded on a public timeline               |

---

## How fatigue is detected

Night Watch does not guess from a single blink. It watches a rolling window of signals from your face and posture:

- **Eye openness** (PERCLOS, blink duration), corrected for head angle so looking sideways does not fake drowsiness
- **Head nods and yawns**
- **Slump** (neck-torso angle when body pose is visible)
- **Stillness and movement** over time

These roll up into a **RestScore** from 0 to 100, with a confidence indicator. Low confidence means the system holds back rather than calling you tired when the camera cannot see you clearly.

For sleepers already napping, a separate **breathing check** uses optical flow on the torso to estimate breath rate. It abstains when someone moves or the signal is too weak. This is a verification aid, not a medical device.

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

---

## Technology

| Layer           | What we use                                                                    |
| --------------- | ------------------------------------------------------------------------------ |
| Robot           | Unitree Go2 via DimensionalOS (WebRTC)                                         |
| On-device AI    | Local LLM (Qwen via Ollama) for agent commands; no internet needed in demo     |
| Vision          | YOLOv8-Face detection + MediaPipe face landmarks, OpenCV                       |
| Fatigue scoring | Interpretable thresholds, with optional learned models trained on venue data   |
| Breathing       | Optical-flow + frequency analysis on a still sleeper                           |
| Voice           | Pre-rendered bilingual clips (warm caregiver tone, not runtime text-to-speech) |
| Booth           | Live video streams, score card, thought ticker, leaderboard                    |
| Data            | Consent-gated capture sessions; SQLite ledger for the night's events           |

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

| Service | Path | Port | Role |
| --- | --- | --- | --- |
| **Booth UI** | `client/` (Next.js) | 3000 | Live video, RestScore, thought ticker, ledger |
| **Policy API** | `server/` (FastAPI) | 8000 | Care loop, ledger, camera capture, proxies fatigue data to the UI |
| **Fatigue detection** | `fatigue_fastapi_service/` (FastAPI) | 8001 | Real YOLOv8-Face + MediaPipe perception (EAR/MAR/PERCLOS/head-pose) over WebSocket |

`server/` can run standalone with a scripted stub score (`SCORER_BACKEND=stub`, the default) or stream real webcam frames to the fatigue detection service for live scoring (`SCORER_BACKEND=live`). The stub needs no extra setup; live mode requires the fatigue service to be running first.

**Recommended:** develop on **native Windows** (`C:\Users\...\NightWatch`) for faster file I/O and simpler `pnpm`/`uv` tooling. Keep the repo on an NTFS path, not under `\\wsl$\...`.

**DimOS / Go2 robot work** still requires **WSL Ubuntu** when you integrate the real dog. The web UI and both FastAPI services run fine on Windows alone.

### Prerequisites (Windows)

- [Node.js 20+](https://nodejs.org/) with [pnpm](https://pnpm.io/) (`corepack enable`)
- [Python 3.12](https://www.python.org/) for `server/` (with [uv](https://docs.astral.sh/uv/) if available, or a plain `venv`)
- **Python 3.12** (not 3.13) for `fatigue_fastapi_service/` — MediaPipe does not ship 3.13 wheels yet

### Run locally (Windows PowerShell)

Terminal 0 — Insta360 bridge (port 5556), optional when using `CAMERA_SOURCE=insta360`:

```powershell
# Requires the proprietary SDK in the git-ignored folder (not redistributable).
$env:INSTA360_SDK_ROOT = "C:\Users\ASUS\Documents\Computer Science\NightWatch\Windows_CameraSDK-2.1.1_MediaSDK-3.1.3"
cd insta360_bridge
.\build.ps1
.\build\Release\insta360_bridge.exe --port 5556
```

Set `CAMERA_SOURCE=insta360` in `server/.env.local`. For robot POV, point `ROBOT_CAMERA_URL` at the bridge on the robot laptop. See [`insta360_bridge/README.md`](insta360_bridge/README.md).

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

Open `http://localhost:3000`. Next.js rewrites `/api/*`, `/video_feed/*`, and `/text_stream/*` to the FastAPI server.

Public nap intake (QR flow): open `http://localhost:3000/form`. Answers persist in SQLite (`data/nightwatch.db` by default). The booth page polls pending escort requests in the **Intake queue** panel.

### Verify everything

```powershell
Invoke-WebRequest http://127.0.0.1:8001/health -UseBasicParsing   # fatigue service (if running)
Invoke-WebRequest http://127.0.0.1:8000/api/score -UseBasicParsing
Invoke-WebRequest http://localhost:3000 -UseBasicParsing
```

### API endpoints (C5 contract)

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/video_feed/pov` | GET | Raw camera MJPEG |
| `/video_feed/annotated` | GET | Annotated fatigue overlay MJPEG |
| `/text_stream/thoughts` | GET | SSE stream of policy events |
| `/api/score` | GET | Latest fatigue frame JSON |
| `/api/plan` | GET | Scheduler route with ETAs |
| `/api/ledger` | GET | Nap timeline |
| `/api/leaderboard` | GET | Fatigue leaderboard |
| `/api/adopt` | POST | Adoption form |
| `/api/capture` | POST | Capture session upload |
| `/api/outcome` | POST | Post-wake survey |
| `/api/form/schema` | GET | Intake questionnaire + `session_id` |
| `/api/form/responses` | POST | Submit nap intake (public, no auth) |
| `/api/form/responses/latest` | GET | Latest intake rows for booth |
| `/api/form/responses/pending-escort` | GET | Visitors waiting for robot escort |
| `/api/form/responses/{id}` | PATCH | Mark acknowledged / escorted / declined |

Stub implementations remain available for offline development (`SCORER_BACKEND=stub`). Live mode is the default and requires the fatigue detection service on port 8001.

---

## License

TBD.
