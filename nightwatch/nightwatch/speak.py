"""SpeakSkill with a natural-voice backend chain: kokoro -> edge-tts -> `say`.

Stock SpeakSkill synthesizes through OpenAITTSNode, which follows
OPENAI_BASE_URL to our gateway proxy. The gateway has no /audio/speech
endpoint (404/429, verified July), so every speak() call waited out a
timeout and the voice was robotic anyway. This replaces it with a chain of
Mandarin-capable backends that degrade gracefully as connectivity changes:

  kokoro  Kokoro-82M neural TTS, fully local once the model is cached. Best
          quality, no network at speak time. The model takes seconds to load,
          so it warms on a background thread at start(); until it is warm,
          utterances fall through to the next backend.
  edge    Microsoft Edge neural TTS (zh-CN-XiaoxiaoNeural). Excellent quality
          but needs the network; a bounded timeout keeps a dead venue link
          from ever hanging a speak call.
  say     macOS `say`. Robotic, but local, instant, and always available.
          Last-resort fallback only.

Backend order is chosen by NIGHTWATCH_TTS ("kokoro" | "edge" | "say" |
"auto", default "auto"). Any non-"say" choice keeps `say` as the final
safety net so the robot is never silent.

Playback stays non-blocking for the caller (as the previous `say`-only
implementation was): speak() enqueues and returns immediately while a worker
thread synthesizes and plays. The queue holds at most two pending utterances;
a third drops the oldest, because a robot that queues 30 s of stale speech is
worse than one that skips a line.

Named SpeakSkill so blueprint dedupe replaces the stock module.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque

from dimos.agents.annotation import skill
from dimos.agents.skills.speak_skill import SpeakSkill as _StockSpeakSkill
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

SAY_BIN = shutil.which("say")
AFPLAY_BIN = shutil.which("afplay")

# Kokoro renders 24 kHz mono float audio.
_KOKORO_SAMPLE_RATE = 24000
# A robot that queues 30 s of stale speech is worse than one that skips a line.
_MAX_PENDING = 2
# A dead venue network must never hang a speak call.
_EDGE_TIMEOUT_S = 5.0

_DEFAULT_KOKORO_VOICE = "zf_xiaoxiao"
_DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"


def _chain_for_mode(mode: str | None) -> list[str]:
    """Ordered backends to attempt for one utterance.

    An explicit backend is honored first, but `say` is appended as the final
    fallback (except when `say` itself is the choice) so the robot never goes
    mute if the preferred backend is cold or offline.
    """
    mode = (mode or "auto").strip().lower()
    if mode == "say":
        return ["say"]
    if mode == "edge":
        return ["edge", "say"]
    if mode == "kokoro":
        return ["kokoro", "say"]
    return ["kokoro", "edge", "say"]


# --- backends ---------------------------------------------------------------
#
# Each backend exposes ready() (cheap, non-blocking: is it usable right now?)
# and synth(text, out_path). synth returns a filesystem path for the worker to
# play via afplay, or None if the backend performed its own playback (`say`).
# synth raises on failure so the chain falls through to the next backend.


class _KokoroBackend:
    name = "kokoro"

    def __init__(self, get_pipeline, voice: str) -> None:
        # get_pipeline() returns the warm KPipeline or None while it loads.
        self._get_pipeline = get_pipeline
        self.voice = voice

    def ready(self) -> bool:
        return self._get_pipeline() is not None

    def synth(self, text: str, out_path: str) -> str:
        import numpy as np
        import soundfile as sf

        pipeline = self._get_pipeline()
        if pipeline is None:
            raise RuntimeError("kokoro model not warm")
        chunks = [
            r.audio.detach().cpu().numpy()
            for r in pipeline(text, voice=self.voice, speed=1.0)
            if r.audio is not None
        ]
        if not chunks:
            raise RuntimeError("kokoro produced no audio")
        sf.write(out_path, np.concatenate(chunks), _KOKORO_SAMPLE_RATE)
        return out_path


class _EdgeBackend:
    name = "edge"

    def __init__(self, voice: str, timeout_s: float = _EDGE_TIMEOUT_S) -> None:
        self.voice = voice
        self.timeout_s = timeout_s

    def ready(self) -> bool:
        # No cheap offline probe; the bounded synth timeout is the guard.
        return True

    def synth(self, text: str, out_path: str) -> str:
        import asyncio

        from edge_tts import Communicate

        timeout = int(self.timeout_s) or 1

        async def _run() -> None:
            comm = Communicate(
                text,
                voice=self.voice,
                connect_timeout=timeout,
                receive_timeout=timeout,
            )
            await comm.save(out_path)

        asyncio.run(asyncio.wait_for(_run(), timeout=self.timeout_s))
        return out_path


class _SayBackend:
    name = "say"

    def __init__(self, say_bin: str | None, run_proc, rate: int = 200) -> None:
        self._say_bin = say_bin
        # run_proc(cmd) runs a subprocess to completion, interruptibly.
        self._run_proc = run_proc
        self.rate = rate

    def ready(self) -> bool:
        return bool(self._say_bin)

    def synth(self, text: str, out_path: str) -> None:
        if not self._say_bin:
            raise RuntimeError("say binary not available")
        # Existing behavior: `say` speaks directly (no afplay), so return None.
        self._run_proc([self._say_bin, "-r", str(self.rate), "--", text])
        return None


class SpeakSkill(_StockSpeakSkill):
    @rpc
    def start(self) -> None:
        # Skip stock start(): it builds the OpenAI TTS node + sounddevice
        # output we never use. Module.start() is all that is needed.
        super(_StockSpeakSkill, self).start()

        self._tmpdir = tempfile.mkdtemp(prefix="nightwatch-tts-")
        self._utt_counter = 0
        self._pending: deque[str] = deque()
        self._max_pending = _MAX_PENDING
        self._queue_cv = threading.Condition()
        self._active_proc: subprocess.Popen | None = None
        self._active_proc_lock = threading.Lock()
        self._stopping = False

        # Kokoro model warms on a background thread; ready() gates on it.
        self._kokoro_pipeline = None
        self._kokoro_voice = os.environ.get(
            "NIGHTWATCH_TTS_VOICE", _DEFAULT_KOKORO_VOICE
        )
        self._edge_voice = os.environ.get(
            "NIGHTWATCH_TTS_EDGE_VOICE", _DEFAULT_EDGE_VOICE
        )

        self._backends = {
            b.name: b
            for b in (
                _KokoroBackend(lambda: self._kokoro_pipeline, self._kokoro_voice),
                _EdgeBackend(self._edge_voice),
                _SayBackend(SAY_BIN, self._run_proc),
            )
        }

        self._worker = threading.Thread(
            target=self._run_worker, daemon=True, name="SpeakSkill-worker"
        )
        self._worker.start()

        mode = os.environ.get("NIGHTWATCH_TTS", "auto")
        if _chain_for_mode(mode)[0] == "kokoro":
            threading.Thread(
                target=self._warmup_kokoro, daemon=True, name="SpeakSkill-warmup"
            ).start()

        logger.info(
            "SpeakSkill started",
            mode=mode,
            kokoro_voice=self._kokoro_voice,
            edge_voice=self._edge_voice,
            say_available=bool(SAY_BIN),
        )

    @rpc
    def stop(self) -> None:
        self._stopping = True
        with self._queue_cv:
            self._queue_cv.notify_all()
        # Cut off whatever is currently playing so shutdown is prompt.
        with self._active_proc_lock:
            proc = self._active_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
            self._active_proc = None
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        super(_StockSpeakSkill, self).stop()

    # ---- warmup -------------------------------------------------------------

    def _warmup_kokoro(self) -> None:
        try:
            from kokoro import KPipeline
        except Exception as exc:
            logger.info("kokoro unavailable; skipping warmup", error=str(exc))
            return
        try:
            t0 = time.monotonic()
            pipeline = KPipeline(lang_code="z")
            # First synth pays the lazy voice download + graph warmup; do it now
            # so the first real utterance is fast.
            list(pipeline("你好", voice=self._kokoro_voice, speed=1.0))
            self._kokoro_pipeline = pipeline
            logger.info(
                "kokoro warm",
                load_s=round(time.monotonic() - t0, 2),
                voice=self._kokoro_voice,
            )
        except Exception:
            logger.exception("kokoro warmup failed; will use edge/say")

    # ---- queue + worker -----------------------------------------------------

    def _enqueue(self, text: str) -> None:
        with self._queue_cv:
            while len(self._pending) >= self._max_pending:
                dropped = self._pending.popleft()
                logger.info("SpeakSkill dropping stale utterance", dropped=dropped)
            self._pending.append(text)
            self._queue_cv.notify()

    def _run_worker(self) -> None:
        while True:
            with self._queue_cv:
                while not self._pending and not self._stopping:
                    self._queue_cv.wait()
                if self._stopping:
                    return
                text = self._pending.popleft()
            try:
                self._speak_now(text)
            except Exception:
                logger.exception("SpeakSkill worker error")

    def _run_proc(self, cmd: list[str]) -> None:
        """Run a subprocess to completion, interruptibly via stop()."""
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        with self._active_proc_lock:
            self._active_proc = proc
        try:
            proc.wait()
        finally:
            with self._active_proc_lock:
                if self._active_proc is proc:
                    self._active_proc = None

    def _play(self, path: str) -> None:
        if not AFPLAY_BIN:
            raise RuntimeError("afplay not available")
        self._run_proc([AFPLAY_BIN, path])

    def _next_path(self, ext: str) -> str:
        self._utt_counter += 1
        return os.path.join(self._tmpdir, f"utt-{self._utt_counter}.{ext}")

    def _speak_now(self, text: str) -> str | None:
        """Try each backend in order; return the one that spoke, or None."""
        mode = os.environ.get("NIGHTWATCH_TTS", "auto")
        for name in _chain_for_mode(mode):
            backend = self._backends.get(name)
            if backend is None or not backend.ready():
                continue
            ext = "wav" if name == "kokoro" else "mp3"
            out_path = self._next_path(ext)
            try:
                t0 = time.monotonic()
                produced = backend.synth(text, out_path)
                synth_s = time.monotonic() - t0
            except Exception as exc:
                logger.warning(
                    "TTS backend failed; falling through",
                    backend=name,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            try:
                if produced is not None:
                    self._play(produced)
            except Exception:
                logger.exception("TTS playback failed", backend=name)
                continue
            logger.info(
                "SpeakSkill spoke", backend=name, synth_s=round(synth_s, 3)
            )
            return name
        logger.warning("SpeakSkill: every backend failed", text=text)
        return None

    # ---- skill --------------------------------------------------------------

    @skill
    def speak(self, text: str, blocking: bool = False) -> str:
        """Speak text out loud through the speakers.

        USE THIS TOOL AS OFTEN AS NEEDED. People can't normally see what
        you say in text, but can hear what you speak. Be concise; speech
        takes time. Returns immediately (speech plays in the background).
        """
        if not text or not text.strip():
            return "(nothing to speak)"
        self._enqueue(text)
        return f"Speaking: {text}"
