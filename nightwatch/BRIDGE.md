# Nightwatch <-> Drowsiness Model Bridge

This document is the handoff contract between the Nightwatch robot stack and
the drowsiness (sleep-deprivation) detection model. The model runs as a
separate process on the same laptop. Nothing on the robot side needs to be
imported to start developing against this contract: you watch an HTTP video
feed and you call skills over an HTTP JSON-RPC endpoint.

The robot side now has the full intervention chain implemented (approach,
verified waves, raised camera, and the escort interface). This document
describes exactly where your two models plug in.

Authoritative references (all paths relative to the repo root):
`nightwatch/nightwatch/contracts.py` (frozen dataclasses),
`nightwatch/nightwatch/intervene.py` (the suspicious protocol),
`nightwatch/nightwatch/curiosity.py` (behavior leases / arbitration),
`nightwatch/nightwatch/world_model.py` (sleeping_area tags, `nearest_area`),
`nightwatch/nightwatch/webchat.py` (video feed, `/operator/*` endpoints).

## 0. The two models you own, and where each runs

You are building two models. They attach to the pipeline at two different
points:

1. **Posture model.** Watches the MJPEG feed continuously and decides when a
   subject looks sleepy enough to investigate. When your threshold trips, you
   trigger the **suspicious protocol** (the `potential_detected` skill). This
   is the entry point into the whole chain.
2. **Facial pipeline.** Runs during the suspicious protocol, on the same MJPEG
   feed, while the dog is standing ~1.2 m away with its camera tilted up onto
   the subject's face. That raised camera angle exists specifically to give
   your facial pipeline a clean, near-frontal view. If your facial pipeline
   confirms sustained sleepiness, you trigger the **escort**
   (`escort_to_sleeping_area`).

Both models consume the same feed and both trigger robot behavior the same
way (an HTTP skill call, section 3). Neither model needs to import robot code.

## 1. The full pipeline, as a chain

```
[your POSTURE model]  watches MJPEG @ ~10 Hz
        |  posture-sleepiness score crosses YOUR threshold
        v
call skill  potential_detected            <-- the SUSPICIOUS PROTOCOL
        |  robot takes a priority-40 "intervene" lease, scans for the
        |  subject, approaches to ~1.2 m standoff, tilts camera +0.35 rad,
        |  waves up to 4 firmware-verified times, re-tilting after each wave
        v
[your FACIAL pipeline] runs HERE, on MJPEG, during the protocol window
        |  (the raised camera is aimed at the face for exactly this)
        |  facial pipeline reports SUSTAINED sleepiness above YOUR threshold
        v
call skill  escort_to_sleeping_area        <-- the ESCORT
        |  robot takes an uninterruptible priority-60 "escort" lease and
        |  navigates to the nearest sleeping_area tag
        v
[nearest sleeping_area]  tents auto-tag areas as sleeping_area; tags persist
                         across sessions in the premap (map) frame
```

Step by step:

1. **Posture watches the feed.** Your posture model reads the MJPEG stream at
   `http://localhost:5555/video_feed/camera` (about 10 Hz, latest frame only;
   see section 2). It scores posture-based sleepiness on its own schedule.

2. **Posture trips the suspicious protocol.** When posture sleepiness crosses
   your threshold, call the `potential_detected` skill over the MCP endpoint
   (exact request format in section 3). The call is **synchronous**: it blocks
   for the whole protocol (seconds, not instant) and returns a one-line
   summary. Trigger it from a background thread so your facial pipeline can run
   during the protocol rather than after it.

3. **The dog runs the suspicious protocol.** On accepting the call the robot:
   - takes a priority-40 `intervene` behavior lease, suspending curious
     wandering and person-following for the duration (section 4);
   - scans in place (60 deg steps, up to a full turn) until it sees a subject,
     then approaches to a ~1.2 m standoff (`standoff_m = 1.2`);
   - tilts its body/camera up by +0.35 rad (`body_pitch_rad = 0.35`, clamped to
     +/- 0.4 rad in firmware) to lift the front camera onto the standing
     subject's face;
   - performs up to 4 firmware-verified "Hello" waves, **re-applying the
     +0.35 rad tilt after every wave** (the wave gesture resets body pose, so
     without the re-tilt the face would drop out of frame after the first
     wave).

4. **Your facial pipeline runs during that window.** This is the intended
   window for the facial pipeline: the dog is stationary at ~1.2 m with the
   camera aimed at the face. Read the same MJPEG feed and run your facial
   analysis (PERCLOS, blink duration, micro-nods, etc.) while
   `potential_detected` is executing. The protocol has a bounded, short
   duration and can exit early (section 5), so treat the window as "as long as
   `behavior == intervene`" on `/operator/status`, not a fixed number of
   seconds.

5. **Facial confirmation triggers the escort.** If your facial pipeline
   reports **sustained** sleepiness above your threshold, call
   `escort_to_sleeping_area` (section 3). The robot takes an uninterruptible
   priority-60 `escort` lease (it outranks `intervene`, so it wins even if an
   intervene lease is somehow still live) and navigates to the nearest
   sleeping area.

6. **Destination: nearest sleeping_area.** The escort target is resolved by the
   world model's `nearest_area("sleeping_area", x, y)` from the robot's current
   pose. Sleeping areas are tagged automatically: a stable **tent** detection
   casts a heavy vote (weight 8) that resolves the area to `sleeping_area`, and
   AprilTag id 2 is named `sleeping_area` by default. These tags persist across
   sessions: once relocalization locks, each area's center is stamped into the
   persistent map frame, so `nearest_area` can return a sleeping area first
   observed in an earlier run.

## 2. Getting camera frames

The robot camera is exposed over plain HTTP while the stack runs:

- MJPEG stream: `http://localhost:5555/video_feed/camera` (about 10 Hz,
  latest frame only, JPEG at the Go2 front-camera resolution, 1280x720).
  Served by `NightwatchWebInput`, throttled by `WEB_CAMERA_HZ = 10.0` in
  `webchat.py`. Each multipart part carries `X-Capture-Timestamp` and
  `X-Frame-Age-Ms` headers so you can drop stale frames.
- The stream retains exactly one latest frame; a slow reader cannot build a
  backlog and cannot stall the robot's other vision consumers.
- The robot moves while scanning. Expect motion blur during exploration and
  approach; gate your model on frame quality. Frames are freshest and most
  stable during the suspicious protocol (the dog is stationary at standoff).
- Robot state for gating: `GET http://localhost:5555/operator/status` returns
  JSON including `behavior`, `map_phase`, `battery_soc`, `navigation`,
  `camera_age_ms`, `owner`, `hold_reason`, and world/person counts.

Quick test while the stack is running:

```bash
curl -s http://localhost:5555/operator/status | python3 -m json.tool
open http://localhost:5555/video_feed/camera
```

In-process alternative: subscribe to the `color_image` stream directly, the
way the Nightwatch modules do (an `In[Image]` port, e.g. in
`nightwatch/nightwatch/intervene.py` and `vision.py`). The HTTP feed is the
intended zero-coupling path and is what the rest of this document assumes.

## 3. How to invoke a skill from outside

Robot behaviors are exposed as **skills**, and the stack serves them over a
single MCP (Model Context Protocol) JSON-RPC endpoint. This is the same
endpoint the robot's own agent uses internally, so calling it from your
process is a first-class path, not a side door.

- Endpoint: `POST http://localhost:9990/mcp`
  (`global_config.mcp_port` defaults to 9990, `listen_host` 127.0.0.1).
- Protocol: JSON-RPC 2.0 over HTTP. CORS is open (`*`).
- The two calls you need are `tools/list` (discover what is registered) and
  `tools/call` (run a skill).

This mirrors how `webchat.py` drives skills: the operator console posts an
action, which the server turns into a tool call with `{"tool": name, "args":
{...}}`. Over HTTP the wire shape is the JSON-RPC envelope below.

Discover available skills:

```bash
curl -s http://localhost:9990/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool
```

Call a skill (`tools/call`), general shape:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "<skill_name>",
    "arguments": { "<arg>": "<value>" }
  }
}
```

The response wraps the skill's return value as text content:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { "content": [ { "type": "text", "text": "<skill return string>" } ] }
}
```

Refusals also arrive as normal `result` text (not JSON-RPC errors). Watch for
these prefixes, which mean the call did not run:
`"Tool not found: ..."`, `"Cannot start '...'"`, `"Error running tool '...'"`.

Timeouts: the reference client uses a 120 s HTTP timeout, and the server caps
capability waits at 30 s. `potential_detected` runs to completion inside the
call, so set your HTTP timeout to at least 120 s for it.

### The two skills in this pipeline

- **`potential_detected`** (live today). One optional string argument `query`
  (default `"person"`), a description of the subject to approach. Returns a
  one-line summary such as
  `"Suspicious protocol complete: 2 real wave(s), 1 blocked attempt(s), subject looked back. (7.3s, 3 total attempt(s))"`.
  Declared `@skill(uses=[CAP_MOVEMENT])` in `intervene.py`.

- **`escort_to_sleeping_area`** (interface as specified; see note below). No
  arguments. Priority-60 `escort` lease. Internally resolves the destination
  via world-model `nearest_area("sleeping_area", x, y)` from the robot's
  current pose and navigates there.

  Status note: `escort.py` is implemented and `EscortSkill` is registered in
  `nightwatch/nightwatch/blueprints.py`, so `escort_to_sleeping_area` appears
  in `tools/list` on a running stack. It refuses with a clear message when no
  sleeping area is known yet or when a higher-priority owner holds motion.
  Fatigue scores can also be pushed to the dashboard via
  `POST /operator/assessment` (FatigueAssessment field names; latest five are
  shown on the operator page and in `/operator/status`).

Helpful read-only skills you can also call the same way:
`curiosity_status` (motion owner / holds), `world_status` (map + memory
health), `list_semantic_areas` (every inferred area, with a `persistent` flag
and `auto_tag`, so you can confirm a `sleeping_area` exists before escorting),
`list_remembered_people`, `person_memory_status`.

## 4. Lease / priority table, and what "blocked" means

Motion authority is arbitrated by the `CuriositySupervisor`
(`curiosity.py`). Effective priority, highest first:

| Level | Owner | Mechanism |
| --- | --- | --- |
| Above all | Safety holds | Supervisor forces HOLD before honoring any lease: battery unknown, battery low, sensors not ready, or an operator "Hold". These preempt even an escort. |
| Above all | Teleop | Keyboard/web teleop rides `tele_cmd_vel` through MovementManager, an unconditional velocity-level override independent of the lease system. |
| 60 | **escort** | `escort_to_sleeping_area` lease. Outranks intervene. |
| 40 | **intervene** | `potential_detected` lease (`lease_priority = 40`). |
| below | curious follow | Autonomous close-person follow (no lease; runs only when no lease is held). |
| below | curiosity | Base exploration (before MAPPED) or coverage patrol (after MAPPED). Lowest; yields to everything above. |

Lease rules that matter to you:

- A new lease is accepted when its priority is **>= the current top lease**;
  ties go to the newer lease. So escort (60) preempts an active intervene (40),
  but a second intervene cannot preempt an escort.
- `potential_detected` acquiring at 40 is now implemented reality (an earlier
  version of this document listed priority 40 only as a proposal).
- The active lease is surfaced on the single RobotActivity stream as
  `behavior = "intervene"` or `"escort"` with `owner = "intervention"` (or the
  escort owner). It shows up on `/operator/status` without any second writer.

**"Blocked" means refused, never queued.** Your calls do not wait in line. If
motion is unavailable you get an immediate refusal string back, and nothing
happens. Two independent gates can refuse you:

1. **Movement capability (MCP server layer).** All movement skills share one
   `"movement"` capability. The server auto-preempts the base curiosity's
   exploration and patrol when you call a movement skill, so those never block
   you. But an active **person-follow** (`follow_person`, which the curious
   close-person follow uses) holds `"movement"` and is **not** auto-preempted.
   In that case `potential_detected` returns
   `"Cannot start 'potential_detected': capability 'movement' is held by 'follow_person'. Call the appropriate stop tool first, then retry."`
   Workaround: call `stop_following` first, then retry `potential_detected`.
   (See the interface-gap note at the end.)

2. **Behavior lease (supervisor layer).** Once past the capability gate,
   `potential_detected` tries to take the intervene lease. If a higher-priority
   lease (an escort at 60) is active, it returns
   `"Cannot start the suspicious protocol: <owner> currently owns motion at a higher priority."`
   A safety hold likewise wins: the supervisor holds regardless of your lease,
   so a low battery mid-protocol stops motion.

Because refusals come back as ordinary `result` text, always inspect the
returned string; do not assume success from HTTP 200.

## 5. Wave protocol semantics you can rely on

Implemented in `run_wave_protocol` / `_run_protocol` in `intervene.py`. These
are guarantees your facial pipeline can time against:

- **4 real waves maximum.** `max_waves = 4`. Only firmware-acknowledged waves
  count. The Go2 silently drops sport gestures unless it is in a
  locomotion-ready FSM, so each wave is re-armed and verified against the
  firmware ack.
- **Blocked attempts are retried, not counted.** A wave the firmware rejects
  returns "not real", triggers a re-arm, and does **not** increment the wave
  count. Blocked attempts are reported separately (`blocked_attempts`).
- **Total attempt cap 8.** `max_wave_attempts = 8` bounds real + blocked
  attempts together, so a permanently-blocked firmware can never spin forever.
- **Early exit on eye contact.** After each real wave the robot runs a gaze
  check (a frontal-face streak: nose and both eyes confidently detected with
  the eyes straddling the nose, held for `gaze_frames_required = 3` consecutive
  frames within a `gaze_window_s = 3.0` s window). The first time it passes,
  the robot performs exactly **one** more real wave (which counts toward the
  cap) and then stops. A single glance does not qualify; the streak resets on
  any non-frontal frame.
- **Camera tilt is re-applied after every real wave** (+0.35 rad). A failed or
  rejected re-tilt is logged and the protocol continues; it never aborts on the
  tilt.
- **Bounded duration.** The protocol is bounded by the approach timeout
  (`approach_timeout_s = 20`), the in-place scan (up to 6 steps of ~1 s), and
  at most 8 attempts each followed by a gaze window of up to 3 s. It always
  ends well inside the 120 s HTTP timeout, and it **always** restores the
  camera to neutral pitch and **always** releases the intervene lease, even if
  the wave loop raises.

Practical guidance for the facial pipeline: start analyzing as soon as you
fire `potential_detected`, and treat the intervention window as open while
`/operator/status` reports `behavior == "intervene"`. Do not wait for the
`potential_detected` response to begin, and do not assume a fixed wave count:
the subject looking back ends the protocol early.

## 6. The FatigueAssessment contract (frozen, unchanged)

Emit one `FatigueAssessment` per scored observation window. The schema is
`nightwatch/nightwatch/contracts.py::FatigueAssessment` (a frozen dataclass):

| Field | Type | Semantics |
| --- | --- | --- |
| `assessment_id` | str | Unique id per assessment (uuid4 hex is fine). |
| `ts` | float | Unix seconds when the window ended. |
| `track_id` | str | Anonymous tracker id of the person in this frame stream. |
| `anonymous_person_id` | str or null | Only when re-identification is confident; else null. |
| `bbox` | [x1, y1, x2, y2] or null | Pixel bbox of the face/person scored. |
| `fatigue_score` | float | Model-defined normalized score, 0.0 to 1.0. |
| `confidence` | float | Model confidence in this score, 0.0 to 1.0. |
| `quality` | float | Input quality (sharpness, face geometry), 0.0 to 1.0. |
| `factors` | [str] | Model explanation (e.g. "perclos", "nodding"). Not a diagnosis. |
| `observation_seconds` | float | Length of the evidence window behind this score. |
| `model_version` | str | Your model/version tag. |

Rules the robot policy enforces (so your side does not have to):

- One frame is never sufficient for an intervention. Score over a window and
  report `observation_seconds` honestly.
- `factors` is an explanation, never a medical claim. The robot's only
  intervention line is: "You look tired. Would you like me to guide you to the
  rest area?"

Example payload:

```json
{
  "assessment_id": "9f2c4a1e0b7d4c9b",
  "ts": 1753300000.0,
  "track_id": "12",
  "anonymous_person_id": null,
  "bbox": [412.0, 96.0, 578.0, 331.0],
  "fatigue_score": 0.82,
  "confidence": 0.71,
  "quality": 0.66,
  "factors": ["perclos", "long_blinks"],
  "observation_seconds": 12.5,
  "model_version": "sleepy-v0.3"
}
```

### Where assessments should be surfaced

The operator dashboard is `http://localhost:5555/operator`; its live status is
`GET http://localhost:5555/operator/status`. That status already exposes the
`behavior` field, which reads `intervene` while the suspicious protocol runs
and `escort` during an escort, so an operator can see the chain fire in real
time. The integrated policy server POSTs every assessment to
`http://localhost:5555/operator/assessment`. The robot keeps a bounded ring and
surfaces the latest five records plus `assessment_count` in
`/operator/status`; the operator page renders them next to `behavior`. The
policy server also keeps JSON lines as a replay/audit trail.

## 7. Worked examples

Assume the stack is running (`source robot.env && dimos run nightwatch.scout`).

### (a) Trigger `potential_detected` (the suspicious protocol)

Shell:

```bash
curl -s http://localhost:9990/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"potential_detected","arguments":{"query":"person"}}}'
# -> {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text",
#     "text":"Suspicious protocol complete: 2 real wave(s), 0 blocked attempt(s),
#             subject looked back. (6.8s, 2 total attempt(s))"}]}}
```

Python (start it in a thread so your facial pipeline runs during the window):

```python
import threading
import httpx

MCP = "http://localhost:9990/mcp"

def call_skill(name, arguments=None, timeout=125.0):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    r = httpx.post(MCP, json=body, timeout=timeout)
    r.raise_for_status()
    parts = r.json()["result"].get("content", [])
    text = "".join(p["text"] for p in parts if p.get("type") == "text")
    refused = text.startswith(("Tool not found:", "Cannot start '", "Error running tool '"))
    return (not refused), text

# Posture threshold tripped: fire the protocol without blocking your loop.
def run_suspicious_protocol():
    ok, msg = call_skill("potential_detected", {"query": "person"})
    print("intervene ->", ok, msg)

threading.Thread(target=run_suspicious_protocol, daemon=True).start()
# ... meanwhile, your FACIAL pipeline keeps reading the MJPEG feed ...
```

### (b) Trigger `escort_to_sleeping_area` (after facial confirmation)

```python
# Facial pipeline confirmed sustained sleepiness.
ok, msg = call_skill("escort_to_sleeping_area")   # no arguments
print("escort ->", ok, msg)
# Before relying on it, confirm a target exists: call_skill via tools/call
#   name="list_semantic_areas" and look for a row whose auto_tag or area_type
#   is "sleeping_area". With none known, the skill refuses with a clear
#   message instead of navigating.
```

Shell equivalent:

```bash
curl -s http://localhost:9990/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"escort_to_sleeping_area","arguments":{}}}'
```

### (c) Read `/operator/status` during both

```bash
# Poll while the protocol runs; behavior flips intervene -> (escort) -> back.
watch -n1 "curl -s http://localhost:5555/operator/status \
  | python3 -c 'import sys,json; s=json.load(sys.stdin); \
      print(s[\"behavior\"], s[\"owner\"], s.get(\"map_phase\"), s.get(\"navigation\"))'"
```

Python poller you can run alongside the trigger:

```python
import time, httpx
STATUS = "http://localhost:5555/operator/status"
for _ in range(30):
    s = httpx.get(STATUS, timeout=5.0).json()
    print(s["behavior"], "| owner", s["owner"],
          "| map", s.get("map_phase"), "| nav", s.get("navigation"),
          "| cam_age_ms", s.get("camera_age_ms"))
    time.sleep(1.0)
# Expect behavior == "intervene" during (a), "escort" during (b),
# and "explore"/"patrol"/"follow" otherwise.
```

## 8. Thresholds (initial proposal, tune together)

The exact score/confidence/quality/window thresholds are yours to own and to
tune with us; the frozen part is the `FatigueAssessment` schema (section 6) and
the trigger mechanics (sections 3 to 5). A reasonable starting policy:
treat an assessment as an intervention candidate only when `fatigue_score`,
`confidence`, `quality`, and `observation_seconds` all clear your bars over
several consecutive windows for the same `track_id`, with a per-person cooldown
so you do not re-trigger on the same subject repeatedly.

Robot-side preconditions worth gating on before you fire a trigger:

- A reachable standoff (~1.2 m) exists; the protocol will still wave from where
  it stands if it cannot reach the exact standoff.
- No higher-priority behavior or safety hold is active (otherwise the call is
  refused; see section 4). Check `behavior`/`hold_reason` on `/operator/status`.
- Mapping maturity: `potential_detected` and `escort_to_sleeping_area` do
  **not** themselves gate on `map_phase`. If you want interventions to stay
  shadow-only until the venue is mapped, gate your own trigger on
  `map_phase == "MAPPED"` from `/operator/status`. The real phase values are
  `NOT_READY`, `EXPLORING`, then `MAPPED` (there is no phase literally named
  "MAPPING"). Escort in particular needs a `sleeping_area` tag to exist, which
  only happens once the dog has seen a tent / sleeping evidence or an
  AprilTag 2, so gating escort on `MAPPED` is the safe default.

## 9. Person linkage / identity (unchanged)

`track_id` values on your assessments come from the same YOLO / BoT-SORT
tracker the robot uses for following. To link an anonymous track to a
remembered person, use the consent-gated identity skills:
`list_remembered_people` and `person_memory_status` in
`nightwatch/nightwatch/follow.py`, backed by the identity store in
`nightwatch/nightwatch/identity.py`. Populate `anonymous_person_id` only when
re-identification is confident; leave it null otherwise.
