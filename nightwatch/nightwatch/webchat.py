"""WebInput that actually SHOWS agent responses.

Stock dimos WebInput (agents/web_human_input.py) wires browser text/voice
INTO the agent but hands the web page's "agent_responses" stream a fresh
empty Subject that nothing ever publishes to, so the chat page shows no
replies (verified: agent answered in the logs while the page stayed blank).

This subclass reimplements start() with three additions:
- an `agent` In port (autoconnects to McpClient's agent Out) whose messages
  are pushed into the page's response stream,
- the robot camera exposed as MJPEG at /video_feed/camera (~10 Hz, the
  WEB_CAMERA_HZ throttle), both for the operator page and for automated
  verification (curl a frame, look at it). The stock module never passes
  any video stream to the web server.

Browser speech recognition is intentionally disabled in this stack. Loading
Whisper continuously consumed CPU even with no microphone input, competing
with camera, mapping, and tracking. The hackathon interaction uses NFC/web
forms in the noisy venue instead.
"""

import asyncio
from collections import deque
from collections.abc import Callable
import html
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time
from typing import Any, Protocol

import cv2
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from langchain_core.messages import BaseMessage
import reactivex as rx
from reactivex.disposable import Disposable, SingleAssignmentDisposable

from dimos.agents.agent_spec import AgentSpec
from dimos.agents.web_human_input import WebInput
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.stream import In, Out
from dimos.core.transport_factory import make_transport
from dimos.msgs.sensor_msgs.Image import Image
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.navigation.base import NavigationState
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger
from dimos.web.robot_web_interface import RobotWebInterface

WEB_CAMERA_HZ = 10.0
WEB_CAMERA_WIDTH = 960
WEB_CAMERA_HEIGHT = 540
WEB_CAMERA_JPEG_QUALITY = 72
STATUS_CACHE_TTL_S = 0.5
STATUS_STALE_AFTER_S = 2.0
STATUS_REFRESH_TIMEOUT_S = 2.0
MAX_ABANDONED_STATUS_REFRESHES = 2
MAX_CAMERA_FRAME_AGE_S = 2.0

# Ring buffer of the most recent FatigueAssessment POSTs the drowsiness models
# push to /operator/assessment. Bounded so a chatty model can never grow memory.
_ASSESSMENT_RING = 50
# The subset of FatigueAssessment (nightwatch/contracts.py) the ingest requires;
# extras are tolerated and stored verbatim so richer payloads survive round-trip.
_ASSESSMENT_REQUIRED = ("assessment_id", "ts", "track_id", "fatigue_score", "confidence")
_ASSESSMENT_NUMERIC = ("ts", "fatigue_score", "confidence")

logger = setup_logger()


def _camera_frame_is_current(
    capture_ts: float,
    last_capture_ts: float,
    *,
    now: float | None = None,
) -> bool:
    """Reject delayed/replayed images before they replace the live camera."""

    wall_time = time.time() if now is None else float(now)
    return (
        math.isfinite(capture_ts)
        and capture_ts > last_capture_ts
        and capture_ts >= wall_time - MAX_CAMERA_FRAME_AGE_S
    )


def _project_cached_status(
    cached: dict[str, Any],
    cache_at_monotonic: float,
    *,
    now_monotonic: float | None = None,
    now_wall: float | None = None,
) -> dict[str, Any]:
    """Project time-dependent fields in a cached snapshot to the present.

    Robot status refreshes deliberately run off the HTTP event loop because an
    optional RPC can block.  When one does block, the last useful status remains
    available, but an age calculated inside that old snapshot must not appear
    frozen.  A private capture timestamp gives camera age an exact live value;
    legacy snapshots without it are advanced by the cache age.
    """

    monotonic_now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    wall_now = time.time() if now_wall is None else float(now_wall)
    projected = dict(cached)
    status_age_ms = max(0.0, (monotonic_now - cache_at_monotonic) * 1000.0)
    projected["status_age_ms"] = status_age_ms
    projected["status_stale"] = status_age_ms >= STATUS_STALE_AFTER_S * 1000.0

    capture_ts = projected.pop("_camera_capture_ts", None)
    if isinstance(capture_ts, (int, float)) and math.isfinite(float(capture_ts)):
        projected["camera_age_ms"] = (
            max(0.0, (wall_now - float(capture_ts)) * 1000.0)
            if float(capture_ts) > 0.0
            else None
        )
    else:
        cached_camera_age = projected.get("camera_age_ms")
        if isinstance(cached_camera_age, (int, float)) and math.isfinite(
            float(cached_camera_age)
        ):
            projected["camera_age_ms"] = max(
                0.0,
                float(cached_camera_age) + status_age_ms,
            )
    return projected


def _validate_assessment(data: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Minimally validate a FatigueAssessment POST body; never raise.

    The body must be a JSON object carrying the core fields every drowsiness
    model sends (``assessment_id``, ``ts``, ``track_id``, ``fatigue_score``,
    ``confidence``); the numeric fields must be finite numbers. Extra fields are
    kept verbatim. Returns ``(record, None)`` on success or ``(None, reason)``
    on rejection, so the caller can answer 400 with a human-readable reason.
    """
    if not isinstance(data, dict):
        return None, "body must be a JSON object"
    missing = [k for k in _ASSESSMENT_REQUIRED if k not in data or data[k] is None]
    if missing:
        return None, f"missing required field(s): {', '.join(missing)}"
    record = dict(data)  # tolerate and keep extras
    for key in _ASSESSMENT_NUMERIC:
        try:
            value = float(record[key])
        except (TypeError, ValueError):
            return None, f"field '{key}' must be a number"
        if not math.isfinite(value):
            return None, f"field '{key}' must be a finite number"
        record[key] = value
    record["assessment_id"] = str(record["assessment_id"])
    record["track_id"] = str(record["track_id"])
    return record, None

_OPERATOR_ACTIONS: dict[str, dict[str, Any]] = {
    "hold": {
        "tool": "hold_position",
        "args": {"reason": "operator console"},
    },
    "resume": {"tool": "resume_curiosity", "args": {}},
    "lie_down": {"tool": "lie_down_until_resumed", "args": {}},
    "stand_up": {"tool": "stand_up_and_resume", "args": {}},
    "wave": {
        "tool": "perform_dog_expression",
        "args": {"expression": "Hello"},
    },
    "stretch": {
        "tool": "perform_dog_expression",
        "args": {"expression": "Stretch"},
    },
    "tail_wag": {
        "tool": "perform_dog_expression",
        "args": {"expression": "WiggleHips"},
    },
    "happy": {
        "tool": "perform_dog_expression",
        "args": {"expression": "Content"},
    },
    "paw": {
        "tool": "perform_dog_expression",
        "args": {"expression": "Scrape"},
    },
    "sit": {
        "tool": "perform_dog_expression",
        "args": {"expression": "Sit"},
    },
    "sleep_area": {"tool": "escort_to_sleeping_area", "args": {}},
    "stop": {"tool": "stop_navigation", "args": {}},
    "stop_follow": {"tool": "stop_following", "args": {}},
    # Voice presets: each plays one canned line through the speak skill.
    # Speech is queued robot-side (non-blocking, max 2 pending) so these can
    # never stall or interrupt other console actions.
    "speak_invite": {
        "tool": "speak",
        "args": {
            "text": (
                "你看起来有点累，需要我带你去休息区吗？ "
                "You look tired. Would you like me to guide you to the rest area?"
            )
        },
    },
    "speak_prescribe": {
        "tool": "speak",
        "args": {
            "text": (
                "建议你去休息区睡一觉。愿意的话，扫一下我身上的二维码。 "
                "A short nap would help. Scan the QR code on my back if you'd like."
            )
        },
    },
    "speak_escort_start": {
        "tool": "speak",
        "args": {
            "text": (
                "跟我来，我带你去休息区。 "
                "Follow me, I'll take you to the rest area."
            )
        },
    },
    "speak_arrival": {
        "tool": "speak",
        "args": {"text": "我们到了，好好休息。 Here we are. Rest well."},
    },
    "speak_farewell": {
        "tool": "speak",
        "args": {
            "text": (
                "别太累了，记得休息。再见！ "
                "Please remember to rest. Goodbye!"
            )
        },
    },
    "speak_greeting": {
        "tool": "speak",
        "args": {"text": "你好，我是守夜犬。 Hello, I'm Night Watch."},
    },
}

# Free-text speech from the operator console is trimmed and hard-capped so a
# pasted essay cannot monopolize the speech queue.
_SPEAK_TEXT_MAX_CHARS = 200

_DIRECT_OPERATOR_ACTIONS = frozenset(
    {
        "operating_mode",
        "mission_mode",
        "control_mode",
        "teleop",
        "emergency_stop",
        "scan_now",
        "scan_settings",
        "speak_text",
    }
)

_OPERATOR_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Nightwatch Operator</title>
  <style>
    :root { color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #091016; color: #e8f1f5; }
    main { width: min(1180px, 100%); margin: auto; padding: 16px; }
    header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
    h1 { margin: 0 0 12px; font-size: 1.35rem; }
    .online { color: #65e6a4; font-size: .85rem; }
    .grid { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(320px, .7fr); gap: 14px; }
    .panel { background: #111c24; border: 1px solid #243642; border-radius: 14px; padding: 12px; }
    img { width: 100%; display: block; border-radius: 9px; background: #050809; aspect-ratio: 16/9; object-fit: contain; }
    .actions { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 12px; }
    .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 7px; margin: 0 0 10px; }
    .metric { background: #091117; border-radius: 8px; padding: 7px 8px; min-width: 0; }
    .metric span { display: block; color: #78909b; font-size: .68rem; text-transform: uppercase; }
    .metric b { display: block; overflow: hidden; text-overflow: ellipsis; font-size: .86rem; }
    .assess { background: #091117; border-radius: 8px; padding: 8px 10px; margin: 0 0 10px; }
    .assess-head { display: flex; justify-content: space-between; align-items: baseline; color: #78909b; font-size: .68rem; text-transform: uppercase; margin-bottom: 4px; }
    .assess-head b { color: #e8f1f5; font-size: .8rem; }
    .assess-list { list-style: none; margin: 0; padding: 0; font: .8rem/1.5 ui-monospace, monospace; }
    .assess-list li { display: flex; justify-content: space-between; gap: 8px; }
    .assess-empty { color: #56707c; }
    .assess-score { color: #ffcf6b; font-weight: 650; }
    button { min-height: 44px; border: 0; border-radius: 9px; background: #244456; color: white; font-weight: 650; cursor: pointer; }
    button:hover { background: #306079; }
    button:disabled { cursor: not-allowed; opacity: .38; }
    button.danger { background: #8a3434; }
    button.primary { background: #087d62; }
    button.active { outline: 3px solid #ffcf6b; outline-offset: -3px; }
    .command-bar { position: sticky; top: 0; z-index: 20; display: grid; grid-template-columns: 1fr auto; gap: 10px; padding: 10px; margin: 0 0 14px; background: #111c24ee; border: 1px solid #34505e; border-radius: 12px; backdrop-filter: blur(12px); }
    .modes { display: grid; grid-template-columns: repeat(3, minmax(130px, 1fr)); gap: 8px; }
    .estop { min-width: 150px; background: #b6252b !important; font-size: .9rem; }
    .manual-pad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 0 0 10px; }
    .manual-pad button { touch-action: none; user-select: none; }
    .manual-pad .wide { grid-column: span 3; }
    .mode-note { grid-column: 1 / -1; color: #91aeba; font: .75rem/1.35 ui-monospace, monospace; }
    #status { min-height: 22px; color: #91aeba; font-size: .86rem; margin: 4px 2px 10px; }
    #chat { height: 290px; overflow: auto; white-space: pre-wrap; background: #091117; border-radius: 9px; padding: 10px; font: .9rem/1.4 ui-monospace, monospace; }
    .user { color: #7ed8ff; margin-top: 8px; }
    .robot { color: #d9e5ea; margin-top: 8px; }
    form { display: flex; gap: 8px; margin-top: 8px; }
    input { flex: 1; min-width: 0; padding: 11px; border-radius: 9px; border: 1px solid #34505e; background: #081117; color: white; font-size: 16px; }
    form button { padding: 0 18px; }
    .hint { color: #78909b; font-size: .78rem; margin: 8px 2px 0; }
    @media (max-width: 800px) { .command-bar { grid-template-columns: 1fr; } .modes { grid-template-columns: 1fr; } .estop { width: 100%; } .grid { grid-template-columns: 1fr; } #chat { height: 230px; } }
  </style>
</head>
<body>
<main>
  <header><h1>Nightwatch Operator</h1><span class="online">● direct robot link</span></header>
  <section class="command-bar">
    <div class="modes">
      <button id="modeAutonomous" onclick="setMode('autonomous')">Autonomous</button>
      <button id="modeSleep" onclick="setMode('sleep_analysis')">Sleep Analysis</button>
      <button id="modeManual" onclick="setMode('manual')">Manual Override</button>
    </div>
    <button class="estop" onclick="act('emergency_stop')">EMERGENCY STOP</button>
    <div class="mode-note">Mode <b id="operatingMode">—</b> · epoch <b id="modeEpoch">—</b> · interaction <b id="interactionState">—</b></div>
  </section>
  <div class="grid">
    <section class="panel">
      <img src="/video_feed/camera" alt="Robot camera">
      <p class="hint">Camera is latest-frame only; slow clients cannot create a video backlog.</p>
    </section>
    <section class="panel">
      <div class="manual-pad" aria-label="Manual movement controls">
        <button class="move" data-x="0" data-y=".45" data-wz="0">Strafe left · Q</button>
        <button class="move" data-x=".55" data-y="0" data-wz="0">Forward · W</button>
        <button class="move" data-x="0" data-y="-.45" data-wz="0">Strafe right · E</button>
        <button class="move" data-x="0" data-y="0" data-wz=".75">Turn left · A</button>
        <button class="move danger" data-x="0" data-y="0" data-wz="0">Stop · Space</button>
        <button class="move" data-x="0" data-y="0" data-wz="-.75">Turn right · D</button>
        <button class="move wide" data-x="-.45" data-y="0" data-wz="0">Backward · S</button>
      </div>
      <div class="actions">
        <button class="primary" onclick="act('scan_now')">Scan now</button>
        <button onclick="location.href='http://localhost:3000/lidar'">Open map &amp; zones</button>
        <button class="danger" onclick="act('hold')">Hold</button>
        <button class="primary" onclick="act('resume')">Resume curious</button>
        <button class="danger" onclick="act('lie_down')">Lie down &amp; hold</button>
        <button class="primary" onclick="act('stand_up')">Stand &amp; resume</button>
        <button onclick="act('wave')">Wave</button>
        <button onclick="act('tail_wag')">Tail wag</button>
        <button onclick="act('stretch')">Play bow</button>
        <button onclick="act('paw')">Paw scrape</button>
        <button onclick="act('sit')">Sit briefly</button>
        <button onclick="act('happy')">Happy wiggle</button>
        <button onclick="act('sleep_area')">Take me to the sleeping area</button>
        <button onclick="act('stop_follow')">Stop following</button>
        <button class="danger" onclick="act('stop')">Stop navigation</button>
      </div>
      <div class="metrics">
        <div class="metric"><span>Behavior</span><b id="behavior">—</b></div>
        <div class="metric"><span>Owner</span><b id="owner">—</b></div>
        <div class="metric"><span>Battery</span><b id="battery">—</b></div>
        <div class="metric"><span>Map</span><b id="mapPhase">—</b></div>
        <div class="metric"><span>Navigation</span><b id="navigation">—</b></div>
        <div class="metric"><span>Camera age</span><b id="cameraAge">—</b></div>
        <div class="metric"><span>Areas</span><b id="areas">—</b></div>
        <div class="metric"><span>Objects</span><b id="objects">—</b></div>
        <div class="metric"><span>People</span><b id="people">—</b></div>
      </div>
      <div class="assess">
        <div class="assess-head"><span>Drowsiness assessments</span><b id="assessCount">0</b></div>
        <ul id="assessList" class="assess-list"><li class="assess-empty">None yet.</li></ul>
      </div>
      <div id="status">Ready.</div>
      <div id="chat"></div>
      <form id="ask">
        <input id="query" autocomplete="off" placeholder="Tell the robot what to do…" required>
        <button class="primary">Send</button>
      </form>
      <p class="hint">Buttons call skills directly. Text goes straight to the robot's DimensionalOS agent.</p>
    </section>
  </div>
</main>
<script>
  const statusEl = document.getElementById('status');
  const chat = document.getElementById('chat');
  let statusSnapshot = {};
  let manualSequence = 0;
  let manualVector = {x:0, y:0, wz:0};
  let manualWasMoving = false;
  let manualSending = false;
  let manualPending = null;
  const heldKeys = new Set();
  function line(css, text) {
    const div = document.createElement('div'); div.className = css; div.textContent = text;
    chat.appendChild(div); chat.scrollTop = chat.scrollHeight;
  }
  const events = new EventSource('/text_stream/agent_responses');
  events.onmessage = e => line('robot', 'robot > ' + e.data);
  events.onerror = () => { statusEl.textContent = 'Response stream reconnecting…'; };
  async function act(action, arguments_ = {}) {
    statusEl.textContent = 'Sending ' + action.replace('_', ' ') + '…';
    try {
      const r = await fetch('/operator/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, arguments: arguments_})});
      const j = await r.json(); statusEl.textContent = j.message;
      return Boolean(j.success);
    } catch (e) { statusEl.textContent = 'Action failed: ' + e.message; }
    return false;
  }
  async function setMode(mode) {
    stopManual();
    await act('operating_mode', {mode});
    await refreshStatus();
  }
  function manualEnabled() {
    return statusSnapshot.operating_mode === 'manual' && Number.isInteger(Number(statusSnapshot.mode_epoch));
  }
  async function sendManual(v) {
    if (!manualEnabled()) return;
    if (manualSending) {
      manualPending = {...v};
      return;
    }
    manualSending = true;
    manualSequence = Math.max(manualSequence, Number(statusSnapshot.manual_input_seq || 0)) + 1;
    try {
      const response = await fetch('/operator/action', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          action:'teleop',
          arguments:{
            epoch:Number(statusSnapshot.mode_epoch),
            sequence:manualSequence,
            x:v.x, y:v.y, wz:v.wz
          }
        })
      });
      if (!response.ok) {
        const body = await response.json();
        statusEl.textContent = body.message || 'Manual command refused.';
      }
    } catch (error) {
      statusEl.textContent = 'Manual link failed; watchdog will stop the robot.';
    } finally {
      manualSending = false;
      if (manualPending) {
        const pending = manualPending;
        manualPending = null;
        void sendManual(pending);
      }
    }
  }
  function stopManual() {
    const wasMoving = manualWasMoving || manualVector.x || manualVector.y || manualVector.wz;
    manualVector = {x:0, y:0, wz:0};
    manualWasMoving = false;
    heldKeys.clear();
    if (wasMoving && manualEnabled()) sendManual(manualVector);
  }
  function vectorFromKeys() {
    let x = 0, y = 0, wz = 0;
    if (heldKeys.has('w')) x += .55;
    if (heldKeys.has('s')) x -= .45;
    if (heldKeys.has('q')) y += .45;
    if (heldKeys.has('e')) y -= .45;
    if (heldKeys.has('a')) wz += .75;
    if (heldKeys.has('d')) wz -= .75;
    const mult = heldKeys.has('shift') ? 1.4 : 1;
    manualVector = {x:x * mult, y:y * mult, wz:wz * mult};
  }
  async function refreshStatus() {
    try {
      const r = await fetch('/operator/status', {cache:'no-store'});
      const s = await r.json();
      if (!s.success) return;
      statusSnapshot = s;
      manualSequence = Math.max(manualSequence, Number(s.manual_input_seq || 0));
      document.getElementById('operatingMode').textContent = s.operating_mode || '—';
      document.getElementById('modeEpoch').textContent = s.mode_epoch == null ? '—' : s.mode_epoch;
      document.getElementById('interactionState').textContent = s.interaction_state || '—';
      document.getElementById('modeAutonomous').classList.toggle('active', s.operating_mode === 'autonomous');
      document.getElementById('modeSleep').classList.toggle('active', s.operating_mode === 'sleep_analysis');
      document.getElementById('modeManual').classList.toggle('active', s.operating_mode === 'manual');
      document.querySelectorAll('.move').forEach(button => button.disabled = s.operating_mode !== 'manual');
      document.getElementById('behavior').textContent = s.behavior || '—';
      document.getElementById('owner').textContent = s.owner || '—';
      document.getElementById('battery').textContent = s.battery_soc == null ? '—' : s.battery_soc + '%';
      document.getElementById('mapPhase').textContent = s.map_phase || '—';
      document.getElementById('navigation').textContent = s.navigation || '—';
      document.getElementById('cameraAge').textContent = s.camera_age_ms == null ? '—' : Math.round(s.camera_age_ms) + ' ms';
      document.getElementById('areas').textContent = s.areas == null ? '—' : s.areas;
      document.getElementById('objects').textContent = s.stable_objects == null ? '—' : s.stable_objects;
      document.getElementById('people').textContent = s.remembered_people == null ? '—' : s.remembered_people;
      document.getElementById('assessCount').textContent = s.assessment_count == null ? '0' : s.assessment_count;
      const assessList = document.getElementById('assessList');
      const items = Array.isArray(s.assessments) ? s.assessments : [];
      if (!items.length) {
        assessList.innerHTML = '<li class="assess-empty">None yet.</li>';
      } else {
        assessList.innerHTML = '';
        for (const a of items) {
          const li = document.createElement('li');
          const who = document.createElement('span');
          who.textContent = 'track ' + (a.track_id == null ? '?' : a.track_id);
          const score = document.createElement('span');
          score.className = 'assess-score';
          const sc = a.fatigue_score == null ? '?' : Number(a.fatigue_score).toFixed(2);
          const age = a.age_s == null ? '?' : Math.round(a.age_s) + 's ago';
          score.textContent = sc + '  ' + age;
          li.appendChild(who); li.appendChild(score);
          assessList.appendChild(li);
        }
      }
      if (s.hold_reason) statusEl.textContent = 'Hold: ' + s.hold_reason;
    } catch (_) {}
  }
  document.querySelectorAll('.move').forEach(button => {
    const start = event => {
      event.preventDefault();
      if (!manualEnabled()) return;
      manualVector = {
        x: Number(button.dataset.x), y: Number(button.dataset.y), wz: Number(button.dataset.wz)
      };
      manualWasMoving = Boolean(manualVector.x || manualVector.y || manualVector.wz);
      sendManual(manualVector);
    };
    button.addEventListener('pointerdown', start);
    button.addEventListener('pointerup', stopManual);
    button.addEventListener('pointercancel', stopManual);
    button.addEventListener('pointerleave', stopManual);
  });
  document.addEventListener('keydown', event => {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
    const key = event.key.toLowerCase();
    if (key === ' ') {
      event.preventDefault();
      stopManual();
      act('emergency_stop');
      return;
    }
    if (!['w','a','s','d','q','e','shift'].includes(key)) return;
    event.preventDefault();
    heldKeys.add(key);
    vectorFromKeys();
  });
  document.addEventListener('keyup', event => {
    const key = event.key.toLowerCase();
    if (!['w','a','s','d','q','e','shift'].includes(key)) return;
    heldKeys.delete(key);
    vectorFromKeys();
    if (!(manualVector.x || manualVector.y || manualVector.wz)) stopManual();
  });
  window.addEventListener('blur', stopManual);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopManual();
  });
  setInterval(() => {
    const moving = Boolean(manualVector.x || manualVector.y || manualVector.wz);
    if (moving && manualEnabled()) {
      manualWasMoving = true;
      sendManual(manualVector);
    }
  }, 100);
  refreshStatus(); setInterval(refreshStatus, 1000);
  document.getElementById('ask').addEventListener('submit', async e => {
    e.preventDefault();
    const input = document.getElementById('query'); const query = input.value.trim();
    if (!query) return; line('user', 'you > ' + query); input.value = '';
    const body = new FormData(); body.append('query', query);
    statusEl.textContent = 'Robot is handling your request…';
    try {
      const r = await fetch('/submit_query', {method:'POST', body});
      const j = await r.json(); statusEl.textContent = j.success ? 'Sent directly to robot AI.' : j.message;
    } catch (err) { statusEl.textContent = 'Send failed: ' + err.message; }
  });
</script>
</body>
</html>"""

# Keep the full workbench and its guide as reviewable assets.  The inline
# console above remains only as a compact recovery page in source history; the
# routes below serve the integrated three-mode workbench.
_OPERATOR_HTML = Path(__file__).with_name("operator_console.html").read_text(
    encoding="utf-8"
)
_OPERATOR_GUIDE_HTML = Path(__file__).with_name("operator_guide.html").read_text(
    encoding="utf-8"
)


class LatestFrameRobotWebInterface(RobotWebInterface):
    """MJPEG server whose clients can never build a stale-frame backlog.

    DimOS' stock server allocates a ten-frame queue and uses blocking ``put``.
    A slow browser can therefore display old frames and eventually block the
    Zenoh image callback that feeds every other vision consumer.  Camera video
    is ephemeral: retain exactly one latest JPEG and discard the replaced one.
    """

    def __init__(
        self,
        *args: Any,
        operator_action: (
            Callable[[str, dict[str, Any] | None], tuple[bool, str]] | None
        ) = None,
        operator_status: Callable[[], dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._operator_action = operator_action
        self._operator_status = operator_status
        # Thread-safe ring buffer of the most recent drowsiness assessments.
        # The POST route (event loop) and the status read (threadpool) both go
        # through the lock, so no reader ever sees a half-written buffer.
        self._assessments: deque[dict[str, Any]] = deque(maxlen=_ASSESSMENT_RING)
        self._assessments_lock = Lock()
        self._status_lock = Lock()
        self._status_cache: dict[str, Any] = {}
        self._status_cache_at = 0.0
        self._status_refreshing = False
        self._status_refresh_started_at = 0.0
        self._status_refresh_generation = 0
        self._status_abandoned_generations: set[int] = set()
        # FastAPIServer creates one global Queue per text stream. Multiple SSE
        # clients then race to consume it, so whichever tab/test reads first
        # steals the robot's answer from every other operator. Dispose that
        # fan-in subscription; text_stream_generator below gives every client
        # its own subscription and queue.
        for disposable in self.text_disposables.values():
            disposable.dispose()
        self.text_disposables.clear()
        self.text_queues.clear()

        @self.app.get("/operator", response_class=HTMLResponse)
        async def operator_console() -> HTMLResponse:
            form_url = html.escape(
                os.environ.get(
                    "NIGHTWATCH_PUBLIC_FORM_URL",
                    "http://localhost:3000/form",
                ),
                quote=True,
            )
            return HTMLResponse(
                _OPERATOR_HTML.replace("__NIGHTWATCH_FORM_URL__", form_url)
            )

        @self.app.get("/operator/help", response_class=HTMLResponse)
        async def operator_guide() -> HTMLResponse:
            return HTMLResponse(_OPERATOR_GUIDE_HTML)

        @self.app.post("/operator/action")
        async def operator_command(request: Request) -> JSONResponse:
            data = await request.json()
            action = str(data.get("action", ""))
            arguments = data.get("arguments")
            if arguments is not None and not isinstance(arguments, dict):
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "message": "arguments must be a JSON object",
                    },
                )
            if (
                action not in _OPERATOR_ACTIONS
                and action not in _DIRECT_OPERATOR_ACTIONS
            ):
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "message": f"Unknown action: {action}"},
                )
            if self._operator_action is None:
                return JSONResponse(
                    status_code=503,
                    content={"success": False, "message": "Operator actions unavailable"},
                )
            ok, message = await asyncio.to_thread(
                self._operator_action,
                action,
                arguments,
            )
            return JSONResponse(
                status_code=200 if ok else 409,
                content={
                    "success": ok,
                    "message": message,
                },
            )

        @self.app.post("/operator/assessment")
        async def operator_assessment(request: Request) -> JSONResponse:
            try:
                data = await request.json()
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "body must be valid JSON"},
                )
            ok, error = self._record_assessment(data)
            if not ok:
                return JSONResponse(
                    status_code=400, content={"ok": False, "error": error}
                )
            return JSONResponse(content={"ok": True})

        @self.app.get("/operator/status")
        async def operator_status() -> JSONResponse:
            if self._operator_status is None:
                return JSONResponse(
                    status_code=503,
                    content={"success": False, "message": "Status unavailable"},
                )
            status, ready = self._cached_operator_status()
            if not ready:
                # Give an inexpensive callback one scheduler turn to prime the
                # cache, without ever waiting on a slow robot RPC.
                await asyncio.sleep(0.02)
                status, ready = self._cached_operator_status()
                if not ready:
                    return JSONResponse(
                        status_code=503,
                        content={"success": False, "message": "Status warming up"},
                    )
            assessments, assessment_count = self._recent_assessments(5)
            return JSONResponse(
                content={
                    "success": True,
                    **status,
                    "assessments": assessments,
                    "assessment_count": assessment_count,
                }
            )

    def _cached_operator_status(self) -> tuple[dict[str, Any], bool]:
        """Return immediately and refresh robot RPCs at most once in flight.

        Status is polled by both the booth and policy bridge. Calling robot,
        map, navigation, and identity RPCs in every HTTP request allowed one
        slow optional worker to pile up requests and starve video delivery.
        """
        now = time.monotonic()
        with self._status_lock:
            ready = bool(self._status_cache)
            cached = dict(self._status_cache)
            cache_at = self._status_cache_at
            due = now - cache_at >= STATUS_CACHE_TTL_S
            refresh_timed_out = (
                self._status_refreshing
                and now - self._status_refresh_started_at >= STATUS_REFRESH_TIMEOUT_S
            )
            if refresh_timed_out:
                # Python cannot cancel a blocked synchronous RPC thread. Mark
                # this generation abandoned so one dead optional worker cannot
                # suppress every later refresh. The small cap prevents an
                # unreachable robot from producing unbounded daemon threads.
                self._status_abandoned_generations.add(
                    self._status_refresh_generation
                )
                self._status_refreshing = False
            if due and not self._status_refreshing:
                if (
                    len(self._status_abandoned_generations)
                    < MAX_ABANDONED_STATUS_REFRESHES
                ):
                    self._status_refreshing = True
                    self._status_refresh_started_at = now
                    self._status_refresh_generation += 1
                    generation = self._status_refresh_generation
                    Thread(
                        target=self._refresh_operator_status,
                        args=(generation,),
                        name=f"Nightwatch-operator-status-{generation}",
                        daemon=True,
                    ).start()
        if ready:
            cached = _project_cached_status(
                cached,
                cache_at,
                now_monotonic=now,
            )
        return cached, ready

    def _refresh_operator_status(self, generation: int) -> None:
        try:
            callback = self._operator_status
            refreshed = dict(callback()) if callback is not None else {}
            if refreshed:
                with self._status_lock:
                    # A late result from an abandoned generation must not
                    # overwrite a newer successful snapshot.
                    if generation == self._status_refresh_generation:
                        self._status_cache = refreshed
                        self._status_cache_at = time.monotonic()
        except Exception:
            logger.exception("Operator status refresh failed")
        finally:
            with self._status_lock:
                was_abandoned = generation in self._status_abandoned_generations
                self._status_abandoned_generations.discard(generation)
                if generation == self._status_refresh_generation and not was_abandoned:
                    self._status_refreshing = False

    def _record_assessment(self, data: Any) -> tuple[bool, str | None]:
        """Validate and append one assessment to the ring buffer, thread-safe.

        Returns ``(True, None)`` when stored, or ``(False, reason)`` when the
        body is rejected. Never raises: a malformed POST becomes a 400.
        """
        record, error = _validate_assessment(data)
        if error is not None:
            return False, error
        record["received_at"] = time.time()
        with self._assessments_lock:
            self._assessments.append(record)
        return True, None

    def _recent_assessments(
        self, limit: int = 5
    ) -> tuple[list[dict[str, Any]], int]:
        """The newest ``limit`` assessments (newest first) and the buffer count.

        Each entry is projected to the operator-relevant fields plus a
        server-computed ``age_s`` (seconds since the assessment's ``ts``), so the
        page never has to reason about client vs robot clock skew.
        """
        now = time.time()
        with self._assessments_lock:
            count = len(self._assessments)
            latest = list(self._assessments)[-limit:]
        latest.reverse()  # newest first
        projected: list[dict[str, Any]] = []
        for rec in latest:
            ts = rec.get("ts")
            age_s = (
                max(0.0, now - float(ts)) if isinstance(ts, (int, float)) else None
            )
            projected.append(
                {
                    "assessment_id": rec.get("assessment_id"),
                    "track_id": rec.get("track_id"),
                    "fatigue_score": rec.get("fatigue_score"),
                    "confidence": rec.get("confidence"),
                    "ts": ts,
                    "age_s": age_s,
                }
            )
        return projected, count

    async def text_stream_generator(self, key: str):  # type: ignore[no-untyped-def]
        """Broadcast each robot response independently to every SSE client."""
        stream = self.text_streams.get(key)
        if stream is None:
            return
        text_queue: Queue[str | None] = Queue(maxsize=100)

        def offer(text: str | None) -> None:
            try:
                text_queue.put_nowait(text)
                return
            except Full:
                pass
            # Bound a stalled client without ever blocking the agent stream.
            try:
                text_queue.get_nowait()
            except Empty:
                pass
            try:
                text_queue.put_nowait(text)
            except Full:
                pass

        disposable = stream.subscribe(
            offer,
            lambda _error: offer(None),
            lambda: offer(None),
        )
        try:
            while True:
                try:
                    text = text_queue.get_nowait()
                except Empty:
                    yield {"event": "ping", "data": ""}
                    await asyncio.sleep(0.1)
                    continue
                if text is None:
                    break
                yield {"event": "message", "id": key, "data": text}
        finally:
            disposable.dispose()

    @staticmethod
    def _offer_latest(
        frame_queue: Queue[Any], frame: tuple[bytes, float] | None
    ) -> None:
        try:
            frame_queue.put_nowait(frame)
            return
        except Full:
            pass
        try:
            frame_queue.get_nowait()
        except Empty:
            pass
        try:
            frame_queue.put_nowait(frame)
        except Full:
            pass

    def process_frame_fastapi(self, frame: tuple[Any, float]) -> tuple[bytes, float]:
        """Encode a frame while retaining its robot capture timestamp."""
        image, capture_ts = frame
        ok, buffer = cv2.imencode(
            ".jpg",
            image,
            [int(cv2.IMWRITE_JPEG_QUALITY), WEB_CAMERA_JPEG_QUALITY],
        )
        if not ok:
            raise ValueError("Could not JPEG-encode camera frame")
        return buffer.tobytes(), float(capture_ts)

    def stream_generator(self, key: str):  # type: ignore[no-untyped-def]
        def generate():  # type: ignore[no-untyped-def]
            frame_queue: Queue[Any] = Queue(maxsize=1)
            self.stream_queues[key] = frame_queue

            if key in self.stream_disposables:
                self.stream_disposables[key].dispose()

            disposable = SingleAssignmentDisposable()
            self.stream_disposables[key] = disposable
            self.disposables.add(disposable)

            if key in self.active_streams:
                disposable.disposable = self.active_streams[key].subscribe(
                    lambda frame: self._offer_latest(frame_queue, frame),
                    lambda _error: self._offer_latest(frame_queue, None),
                    lambda: self._offer_latest(frame_queue, None),
                )

            try:
                while True:
                    try:
                        frame = frame_queue.get(timeout=1)
                    except Empty:
                        continue
                    if frame is None:
                        break
                    jpeg, capture_ts = frame
                    age_ms = max(0.0, (time.time() - capture_ts) * 1000.0)
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"X-Capture-Timestamp: {capture_ts:.6f}\r\n".encode()
                        + f"X-Frame-Age-Ms: {age_ms:.1f}\r\n\r\n".encode()
                        + jpeg
                        + b"\r\n"
                    )
            finally:
                disposable.dispose()

        return generate

    def create_video_feed_route(self, key: str):  # type: ignore[no-untyped-def]
        async def video_feed() -> StreamingResponse:
            return StreamingResponse(
                self.stream_generator(key)(),
                media_type="multipart/x-mixed-replace; boundary=frame",
            )

        return video_feed


def _message_text(msg: Any) -> str | None:
    """Render a langchain BaseMessage for the chat page; None = skip."""
    msg_type = getattr(msg, "type", "")
    if msg_type == "human":
        return None  # the user just typed it; echoing is noise
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )
    text = (content or "").strip()
    if msg_type == "tool":
        return f"[skill] {text}" if text else None
    if msg_type == "ai":
        calls = getattr(msg, "tool_calls", None) or []
        lines = [f"[calling] {c['name']}({c.get('args', {})})" for c in calls]
        if text:
            lines.append(text)
        return "\n".join(lines) if lines else None
    return text or None


class OperatorStatusSpec(Spec, Protocol):
    def curiosity_status(self) -> dict[str, Any]: ...
    def set_operating_mode(self, mode: str) -> str: ...
    def set_mission_mode(self, mode: str) -> str: ...
    def set_control_mode(self, mode: str) -> str: ...
    def note_manual_input(self, epoch: int, sequence: int) -> str: ...
    def request_fatigue_scan(self) -> str: ...
    def set_sleep_scan_settings(
        self, interval_s: float, duration_s: float
    ) -> str: ...
    def hold_position(self, reason: str) -> str: ...
    def resume_curiosity(self) -> str: ...
    def lie_down_until_resumed(self) -> str: ...
    def stand_up_and_resume(self) -> str: ...
    def perform_dog_expression(self, expression: str) -> str: ...


class WorldStatusSpec(Spec, Protocol):
    def world_status(self) -> dict[str, Any]: ...


class PersonMemoryStatusSpec(Spec, Protocol):
    def person_memory_status(self) -> dict[str, Any]: ...


class NightwatchWebInput(WebInput):
    agent: In[BaseMessage]
    color_image: In[Image]
    tele_cmd_vel: Out[Twist]
    _agent_spec: AgentSpec
    _curiosity: OperatorStatusSpec
    _navigation: NavigationInterfaceSpec
    _world: WorldStatusSpec
    _follow: PersonMemoryStatusSpec

    @staticmethod
    def _accepted(message: Any) -> tuple[bool, str]:
        text = str(message)
        refused = any(
            word in text.lower()
            for word in ("refused", "unsupported", "failed", "ignored")
        )
        return not refused, text

    def _publish_manual_twist(
        self,
        *,
        epoch: int,
        sequence: int,
        x: float,
        y: float,
        wz: float,
    ) -> tuple[bool, str]:
        with self._teleop_lock:
            if sequence <= self._last_teleop_seq:
                return False, "Stale manual command ignored."
        try:
            status = self._curiosity.curiosity_status()
            if str(status.get("operating_mode")) != "manual":
                return False, "Select Manual Override first."
            accepted, message = self._accepted(
                self._curiosity.note_manual_input(epoch, sequence)
            )
            if not accepted:
                return False, message
            twist = Twist(
                Vector3(
                    max(-0.8, min(0.8, float(x))),
                    max(-0.5, min(0.5, float(y))),
                    0.0,
                ),
                Vector3(0.0, 0.0, max(-1.0, min(1.0, float(wz)))),
            )
            self.tele_cmd_vel.publish(twist)
            with self._teleop_lock:
                self._last_teleop_seq = sequence
                self._teleop_last_at = time.monotonic()
                self._teleop_active = not twist.is_zero()
            return (
                True,
                "Manual velocity updated."
                if not twist.is_zero()
                else "Robot stopped; Manual Override remains active.",
            )
        except Exception:
            logger.exception("Operator teleop failed")
            try:
                self.tele_cmd_vel.publish(Twist.zero())
            except Exception:
                logger.exception("Operator teleop failure stop failed")
            return False, "Manual command failed; zero velocity was requested."

    def _dispatch_operator_action(
        self, action: str, arguments: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        arguments = arguments or {}
        try:
            if action == "operating_mode":
                self._navigation.cancel_goal()
                self.tele_cmd_vel.publish(Twist.zero())
                return self._accepted(
                    self._curiosity.set_operating_mode(
                        str(arguments.get("mode", ""))
                    )
                )
            if action == "mission_mode":
                self._navigation.cancel_goal()
                self.tele_cmd_vel.publish(Twist.zero())
                return self._accepted(
                    self._curiosity.set_mission_mode(
                        str(arguments.get("mode", ""))
                    )
                )
            if action == "control_mode":
                self._navigation.cancel_goal()
                self.tele_cmd_vel.publish(Twist.zero())
                return self._accepted(
                    self._curiosity.set_control_mode(
                        str(arguments.get("mode", ""))
                    )
                )
            if action == "teleop":
                return self._publish_manual_twist(
                    epoch=int(arguments.get("epoch", -1)),
                    sequence=int(arguments.get("sequence", 0)),
                    x=float(arguments.get("x", 0.0)),
                    y=float(arguments.get("y", 0.0)),
                    wz=float(arguments.get("wz", 0.0)),
                )
            if action == "emergency_stop":
                self._navigation.cancel_goal()
                self.tele_cmd_vel.publish(Twist.zero())
                result = self._curiosity.set_operating_mode("manual")
                with self._teleop_lock:
                    self._teleop_active = False
                    self._teleop_last_at = time.monotonic()
                return True, f"Emergency stop sent. {result}"
            if action == "scan_now":
                return self._accepted(self._curiosity.request_fatigue_scan())
            if action == "scan_settings":
                return self._accepted(
                    self._curiosity.set_sleep_scan_settings(
                        float(arguments.get("interval_s", 25.0)),
                        float(arguments.get("duration_s", 7.0)),
                    )
                )
            direct_curiosity_actions: dict[str, Callable[[], str]] = {
                "hold": lambda: self._curiosity.hold_position(
                    "operator console"
                ),
                "resume": self._curiosity.resume_curiosity,
                "lie_down": self._curiosity.lie_down_until_resumed,
                "stand_up": self._curiosity.stand_up_and_resume,
                "wave": lambda: self._curiosity.perform_dog_expression("Hello"),
                "stretch": lambda: self._curiosity.perform_dog_expression(
                    "Stretch"
                ),
                "tail_wag": lambda: self._curiosity.perform_dog_expression(
                    "WiggleHips"
                ),
                "happy": lambda: self._curiosity.perform_dog_expression(
                    "Content"
                ),
                "paw": lambda: self._curiosity.perform_dog_expression("Scrape"),
                "sit": lambda: self._curiosity.perform_dog_expression("Sit"),
            }
            direct = direct_curiosity_actions.get(action)
            if direct is not None:
                return self._accepted(direct())
        except (TypeError, ValueError):
            return False, "Invalid operator command parameters."
        except Exception:
            logger.exception("Direct operator action failed", action=action)
            try:
                self.tele_cmd_vel.publish(Twist.zero())
            except Exception:
                logger.exception("Direct operator failure stop failed")
            return False, "Direct command failed; zero velocity was requested."

        if action == "speak_text":
            # Free text comes from the console payload; sanitize server-side.
            # Speak is queued and non-blocking robot-side, so dispatching it
            # never interrupts or delays other operator actions.
            text = str(arguments.get("text", "")).strip()
            if not text:
                return False, "Speak text is empty."
            continuation: dict[str, Any] | None = {
                "tool": "speak",
                "args": {"text": text[:_SPEAK_TEXT_MAX_CHARS]},
            }
        else:
            continuation = _OPERATOR_ACTIONS.get(action)
        if continuation is None:
            return False, f"Unknown operator action: {action}"
        try:
            ok = bool(
                self._agent_spec.dispatch_continuation(
                    continuation,
                    {
                        "_silent": True,
                        "label": f"operator:{action}",
                    },
                )
            )
            return (
                ok,
                f"{action.replace('_', ' ').title()} sent."
                if ok
                else f"{action.replace('_', ' ').title()} was refused.",
            )
        except Exception:
            logger.exception("Operator action failed", action=action)
            return False, "Operator action failed."

    def _operator_status(self) -> dict[str, Any]:
        try:
            status = dict(self._curiosity.curiosity_status())
        except Exception:
            logger.exception("Operator curiosity status failed")
            status = {}
        try:
            nav = self._navigation.get_state()
            status["navigation"] = (
                nav.value if isinstance(nav, NavigationState) else str(nav)
            )
        except Exception:
            status["navigation"] = "unavailable"
        capture_ts = getattr(self, "_last_camera_capture_ts", 0.0)
        # Kept private by ``_project_cached_status``. This lets the lightweight
        # HTTP path calculate a live age even if a later optional status RPC
        # blocks the background refresh thread indefinitely.
        status["_camera_capture_ts"] = capture_ts
        status["camera_age_ms"] = (
            max(0.0, (time.time() - capture_ts) * 1000.0)
            if capture_ts
            else None
        )
        now = time.monotonic()
        if now - getattr(self, "_world_status_checked_at", 0.0) >= 5.0:
            try:
                self._cached_world_status = dict(self._world.world_status())
            except Exception:
                logger.exception("Operator world status failed")
                self._cached_world_status = {}
            self._world_status_checked_at = now
        status.update(getattr(self, "_cached_world_status", {}))
        try:
            status.update(self._follow.person_memory_status())
        except Exception:
            logger.exception("Operator person-memory status failed")
        return status

    @rpc
    def start(self) -> None:
        super(WebInput, self).start()  # Module.start; we replace WebInput's body

        self._human_transport = make_transport("/human_input")

        audio_subject: rx.subject.Subject[Any] = rx.subject.Subject()
        responses: rx.subject.Subject[str] = rx.subject.Subject()

        # Robot camera -> MJPEG feed, throttled; server expects BGR numpy.
        camera_frames: rx.subject.Subject[Any] = rx.subject.Subject()
        min_period = 1.0 / WEB_CAMERA_HZ
        last_emit = [0.0]
        self._last_camera_capture_ts = 0.0
        self._world_status_checked_at = 0.0
        self._cached_world_status: dict[str, Any] = {}
        self._teleop_lock = Lock()
        self._teleop_stop_event = Event()
        self._teleop_watchdog_thread: Thread | None = None
        self._last_teleop_seq = 0
        self._teleop_last_at = 0.0
        self._teleop_active = False

        def _on_frame(img: Image) -> None:
            capture_ts = float(img.ts)
            if not _camera_frame_is_current(
                capture_ts,
                self._last_camera_capture_ts,
            ):
                return
            now = time.monotonic()
            if now - last_emit[0] < min_period:
                return
            try:
                frame = img.to_opencv()
                if (
                    frame.shape[1] != WEB_CAMERA_WIDTH
                    or frame.shape[0] != WEB_CAMERA_HEIGHT
                ):
                    frame = cv2.resize(
                        frame,
                        (WEB_CAMERA_WIDTH, WEB_CAMERA_HEIGHT),
                        interpolation=cv2.INTER_AREA,
                    )
                # Advance freshness only after conversion succeeds; a corrupt
                # high-timestamp image must not suppress the next valid frame.
                self._last_camera_capture_ts = capture_ts
                last_emit[0] = now
                camera_frames.on_next((frame, capture_ts))
            except Exception:
                logger.exception("camera frame conversion failed")

        self.register_disposable(Disposable(self.color_image.subscribe(_on_frame)))

        self._web_interface = LatestFrameRobotWebInterface(
            port=5555,
            text_streams={"agent_responses": responses},
            audio_subject=audio_subject,
            camera=camera_frames,
            operator_action=self._dispatch_operator_action,
            operator_status=self._operator_status,
        )

        # browser text -> agent
        self.register_disposable(
            self._web_interface.query_stream.subscribe(self._human_transport.publish)
        )

        # agent -> page (the wire stock WebInput is missing)
        def _on_agent(msg: Any) -> None:
            try:
                text = _message_text(msg)
                if text:
                    responses.on_next(text)
            except Exception:
                logger.exception("Failed to render agent message for web chat")

        self.register_disposable(Disposable(self.agent.subscribe(_on_agent)))

        self._thread = Thread(target=self._web_interface.run, daemon=True)
        self._thread.start()

        def _teleop_watchdog() -> None:
            while not self._teleop_stop_event.wait(0.05):
                with self._teleop_lock:
                    expired = (
                        self._teleop_active
                        and time.monotonic() - self._teleop_last_at > 0.5
                    )
                    if expired:
                        self._teleop_active = False
                if expired:
                    try:
                        self.tele_cmd_vel.publish(Twist.zero())
                    except Exception:
                        logger.exception("Operator teleop watchdog stop failed")

        self._teleop_watchdog_thread = Thread(
            target=_teleop_watchdog,
            name="nightwatch-operator-teleop-watchdog",
            daemon=True,
        )
        self._teleop_watchdog_thread.start()

        logger.info(
            "Nightwatch operator started",
            operator_url="http://localhost:5555/operator",
        )

    @rpc
    def stop(self) -> None:
        stop_event = getattr(self, "_teleop_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        try:
            self.tele_cmd_vel.publish(Twist.zero())
        except Exception:
            logger.exception("Operator shutdown stop failed")
        watchdog = getattr(self, "_teleop_watchdog_thread", None)
        if watchdog is not None:
            watchdog.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        super().stop()
