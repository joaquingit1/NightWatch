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
| Vision          | MediaPipe face landmarks + pose, OpenCV                                        |
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

## License

TBD.
