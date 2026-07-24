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
import math
from queue import Empty, Full, Queue
from threading import Lock, Thread
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
from dimos.core.core import rpc
from dimos.core.stream import In
from dimos.core.transport_factory import make_transport
from dimos.msgs.sensor_msgs.Image import Image
from dimos.navigation.base import NavigationState
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.spec.utils import Spec
from dimos.utils.logging_config import setup_logger
from dimos.web.robot_web_interface import RobotWebInterface

WEB_CAMERA_HZ = 10.0

# Ring buffer of the most recent FatigueAssessment POSTs the drowsiness models
# push to /operator/assessment. Bounded so a chatty model can never grow memory.
_ASSESSMENT_RING = 50
# The subset of FatigueAssessment (nightwatch/contracts.py) the ingest requires;
# extras are tolerated and stored verbatim so richer payloads survive round-trip.
_ASSESSMENT_REQUIRED = ("assessment_id", "ts", "track_id", "fatigue_score", "confidence")
_ASSESSMENT_NUMERIC = ("ts", "fatigue_score", "confidence")

logger = setup_logger()


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
}

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
    button.danger { background: #8a3434; }
    button.primary { background: #087d62; }
    #status { min-height: 22px; color: #91aeba; font-size: .86rem; margin: 4px 2px 10px; }
    #chat { height: 290px; overflow: auto; white-space: pre-wrap; background: #091117; border-radius: 9px; padding: 10px; font: .9rem/1.4 ui-monospace, monospace; }
    .user { color: #7ed8ff; margin-top: 8px; }
    .robot { color: #d9e5ea; margin-top: 8px; }
    form { display: flex; gap: 8px; margin-top: 8px; }
    input { flex: 1; min-width: 0; padding: 11px; border-radius: 9px; border: 1px solid #34505e; background: #081117; color: white; font-size: 16px; }
    form button { padding: 0 18px; }
    .hint { color: #78909b; font-size: .78rem; margin: 8px 2px 0; }
    @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } #chat { height: 230px; } }
  </style>
</head>
<body>
<main>
  <header><h1>Nightwatch Operator</h1><span class="online">● direct robot link</span></header>
  <div class="grid">
    <section class="panel">
      <img src="/video_feed/camera" alt="Robot camera">
      <p class="hint">Camera is latest-frame only; slow clients cannot create a video backlog.</p>
    </section>
    <section class="panel">
      <div class="actions">
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
  function line(css, text) {
    const div = document.createElement('div'); div.className = css; div.textContent = text;
    chat.appendChild(div); chat.scrollTop = chat.scrollHeight;
  }
  const events = new EventSource('/text_stream/agent_responses');
  events.onmessage = e => line('robot', 'robot > ' + e.data);
  events.onerror = () => { statusEl.textContent = 'Response stream reconnecting…'; };
  async function act(action) {
    statusEl.textContent = 'Sending ' + action.replace('_', ' ') + '…';
    try {
      const r = await fetch('/operator/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})});
      const j = await r.json(); statusEl.textContent = j.message;
    } catch (e) { statusEl.textContent = 'Action failed: ' + e.message; }
  }
  async function refreshStatus() {
    try {
      const r = await fetch('/operator/status', {cache:'no-store'});
      const s = await r.json();
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
        operator_action: Callable[[str], bool] | None = None,
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
            return HTMLResponse(_OPERATOR_HTML)

        @self.app.post("/operator/action")
        async def operator_command(request: Request) -> JSONResponse:
            data = await request.json()
            action = str(data.get("action", ""))
            if action not in _OPERATOR_ACTIONS:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "message": f"Unknown action: {action}"},
                )
            if self._operator_action is None:
                return JSONResponse(
                    status_code=503,
                    content={"success": False, "message": "Operator actions unavailable"},
                )
            ok = await asyncio.to_thread(self._operator_action, action)
            return JSONResponse(
                status_code=200 if ok else 409,
                content={
                    "success": ok,
                    "message": (
                        f"{action.replace('_', ' ').title()} sent."
                        if ok
                        else f"{action.replace('_', ' ').title()} was refused."
                    ),
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
            status = await asyncio.to_thread(self._operator_status)
            assessments, assessment_count = self._recent_assessments(5)
            return JSONResponse(
                content={
                    "success": True,
                    **status,
                    "assessments": assessments,
                    "assessment_count": assessment_count,
                }
            )

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
        ok, buffer = cv2.imencode(".jpg", image)
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


class WorldStatusSpec(Spec, Protocol):
    def world_status(self) -> dict[str, Any]: ...


class PersonMemoryStatusSpec(Spec, Protocol):
    def person_memory_status(self) -> dict[str, Any]: ...


class NightwatchWebInput(WebInput):
    agent: In[BaseMessage]
    color_image: In[Image]
    _agent_spec: AgentSpec
    _curiosity: OperatorStatusSpec
    _navigation: NavigationInterfaceSpec
    _world: WorldStatusSpec
    _follow: PersonMemoryStatusSpec

    def _dispatch_operator_action(self, action: str) -> bool:
        continuation = _OPERATOR_ACTIONS.get(action)
        if continuation is None:
            return False
        try:
            return bool(
                self._agent_spec.dispatch_continuation(
                    continuation,
                    {
                        "_silent": True,
                        "label": f"operator:{action}",
                    },
                )
            )
        except Exception:
            logger.exception("Operator action failed", action=action)
            return False

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

        def _on_frame(img: Image) -> None:
            now = time.monotonic()
            if now - last_emit[0] < min_period:
                return
            last_emit[0] = now
            try:
                self._last_camera_capture_ts = float(img.ts)
                camera_frames.on_next((img.to_opencv(), img.ts))
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

        logger.info(
            "Nightwatch operator started",
            operator_url="http://localhost:5555/operator",
        )
