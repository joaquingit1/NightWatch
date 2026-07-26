from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import numpy as np
import websockets
from app.services.live_scorer import LiveScoreSource

_FRAME = np.zeros((48, 64, 3), dtype=np.uint8)


def _result_payload(sequence: int) -> str:
    return json.dumps(
        {
            "type": "result",
            "sequence": sequence,
            "processing_ms": 5.0,
            "frame": {"width": 64, "height": 48},
            "model": {"version": "test-model"},
            "people": [
                {
                    "track_id": 1,
                    "status": "ALERT",
                    "bbox": {
                        "x1": 10,
                        "y1": 10,
                        "x2": 40,
                        "y2": 40,
                        "confidence": 0.9,
                    },
                    "quality": 0.8,
                    "landmarks_detected": True,
                    "calibrating": False,
                    "calibration_progress": 1.0,
                    "state": {"fatigue_score": 12.0},
                }
            ],
        }
    )


@contextlib.asynccontextmanager
async def _fake_model(
    handler: Callable[[websockets.ServerConnection], Awaitable[None]],
) -> AsyncIterator[str]:
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _echo_results(ws: websockets.ServerConnection) -> None:
    await ws.send(json.dumps({"type": "ready"}))
    sequence = 0
    async for _ in ws:
        sequence += 1
        await ws.send(_result_payload(sequence))


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def test_display_analysis_is_always_on_by_default() -> None:
    """No should_analyze gate wired means the scorer analyzes continuously.

    main.py relies on this default so overlays and /api/score stay live in
    every robot operating mode; only RobotBridge gates interventions.
    """

    async def scenario() -> None:
        async with _fake_model(_echo_results) as ws_url:
            source = LiveScoreSource(ws_url=ws_url, get_frame=lambda: _FRAME)
            assert source._should_analyze() is True
            await source.start()
            try:
                await _wait_until(
                    lambda: source.latest().source_status == "live"
                )
                frame = source.latest()
                assert frame.person_id == "track-1"
                assert frame.sequence >= 1
            finally:
                await source.stop()

    asyncio.run(scenario())


def test_model_annotated_frame_stays_paired_with_its_capture() -> None:
    async def scenario() -> None:
        annotated_jpeg = b"\xff\xd8model-overlay\xff\xd9"
        capture_ts = time.time() - 0.125

        async def annotated_echo(ws: websockets.ServerConnection) -> None:
            await ws.send(json.dumps({"type": "ready"}))
            async for _ in ws:
                payload = json.loads(_result_payload(7))
                payload["annotated_frame_follows"] = True
                await ws.send(json.dumps(payload))
                await ws.send(annotated_jpeg)

        async with _fake_model(annotated_echo) as ws_url:
            source = LiveScoreSource(
                ws_url=ws_url,
                get_frame=lambda: (_FRAME, capture_ts),
                request_annotated_frames=True,
            )
            await source.start()
            try:
                await _wait_until(
                    lambda: source.latest_annotated_sample() is not None
                )
                assert source.latest_annotated_sample() == (
                    annotated_jpeg,
                    capture_ts,
                )
                assert source.latest().sequence == 7
            finally:
                await source.stop()

    asyncio.run(scenario())


def test_standby_keeps_ts_fresh_and_resumes_live_when_active() -> None:
    async def scenario() -> None:
        gate = {"analyze": False}
        async with _fake_model(_echo_results) as ws_url:
            source = LiveScoreSource(
                ws_url=ws_url,
                get_frame=lambda: _FRAME,
                should_analyze=lambda: gate["analyze"],
            )
            await source.start()
            try:
                await _wait_until(
                    lambda: source.latest().source_status == "standby"
                )
                first_ts = source.latest().ts
                # Standby must be a live heartbeat, not a frozen snapshot of
                # the moment the gate closed.
                await _wait_until(lambda: source.latest().ts > first_ts)
                assert source.latest().source_status == "standby"

                gate["analyze"] = True
                await _wait_until(
                    lambda: source.latest().source_status == "live"
                )
                assert source.latest().person_id == "track-1"
            finally:
                await source.stop()

    asyncio.run(scenario())


def test_hung_model_becomes_offline_then_reconnects() -> None:
    """A model that accepts frames but never answers must not freeze the
    score: recv timeout marks the source offline, and the next connection is
    retried with growing backoff until results flow again."""

    async def scenario() -> None:
        connections = {"count": 0}

        async def flaky(ws: websockets.ServerConnection) -> None:
            connections["count"] += 1
            if connections["count"] == 1:
                await ws.send(json.dumps({"type": "ready"}))
                # Accept frames but never answer, until the client gives up.
                await ws.wait_closed()
            else:
                await _echo_results(ws)

        async with _fake_model(flaky) as ws_url:
            source = LiveScoreSource(
                ws_url=ws_url,
                get_frame=lambda: _FRAME,
                reconnect_delay=0.05,
                max_reconnect_delay=0.2,
                connect_timeout=1.0,
                response_timeout=0.3,
            )
            await source.start()
            try:
                await _wait_until(
                    lambda: source.latest().source_status == "model_offline"
                )
                assert source._backoff > source._reconnect_delay
                await _wait_until(
                    lambda: source.latest().source_status == "live"
                )
                assert connections["count"] >= 2
                # Backoff resets once a healthy stream is re-established.
                assert source._backoff == source._reconnect_delay
            finally:
                await source.stop()

    asyncio.run(scenario())


def test_app_wiring_keeps_scoring_active_in_every_robot_mode(
    monkeypatch, tmp_path
) -> None:
    """Overlays and /api/score are display-only and must run in every robot
    operating mode; only RobotBridge gates the intervention pipeline."""
    monkeypatch.setenv("SCORER_BACKEND", "live")
    monkeypatch.setenv("CAMERA_SOURCE", "stub")
    monkeypatch.setenv("DEMO_MODE", "live")
    monkeypatch.setenv("ROBOT_BRIDGE_ENABLED", "true")
    # Unroutable local ports: the test must never talk to a real robot stack.
    monkeypatch.setenv("FATIGUE_WS_URL", "ws://127.0.0.1:59921/v1/streams/detect")
    monkeypatch.setenv("ROBOT_STATUS_URL", "http://127.0.0.1:59922/operator/status")
    monkeypatch.setenv("ROBOT_ASSESSMENT_URL", "")
    monkeypatch.setenv("ROBOT_MCP_URL", "http://127.0.0.1:59923/mcp")
    monkeypatch.setenv("ROBOT_ASSESSMENT_PATH", str(tmp_path / "assessments.jsonl"))
    monkeypatch.setenv("INTAKE_DB_PATH", str(tmp_path / "intake.db"))
    monkeypatch.setenv("NIGHTWATCH_ZONE_STATE_PATH", str(tmp_path / "zones.json"))

    from app.main import create_app, lifespan

    app = create_app()

    async def scenario() -> None:
        async with lifespan(app):
            source = app.state.score_source
            assert isinstance(source, LiveScoreSource)
            bridge = app.state.robot_bridge
            assert bridge is not None
            for mode in ("autonomous", "manual", "sleep_analysis", None):
                bridge._latest_status = {
                    "enabled": True,
                    "connected": True,
                    "operating_mode": mode,
                    "behavior": "patrol",
                    "owner": "",
                    "interaction_state": "idle",
                    "scan_active": False,
                    "face_observation_active": False,
                }
                assert source._should_analyze() is True, (
                    f"display analysis must stay on in mode {mode!r}"
                )

    asyncio.run(scenario())


def test_stream_capacity_refusal_is_offline_then_recovers() -> None:
    """A single-stream service that is already occupied answers with an error
    instead of "ready". That must read as model_offline and keep retrying, and
    recover once the slot frees up."""

    async def scenario() -> None:
        connections = {"count": 0}

        async def occupied_then_free(ws: websockets.ServerConnection) -> None:
            connections["count"] += 1
            if connections["count"] == 1:
                await ws.send(
                    json.dumps(
                        {"type": "error", "code": "stream_capacity_reached"}
                    )
                )
                await ws.close(code=1013)
            else:
                await _echo_results(ws)

        async with _fake_model(occupied_then_free) as ws_url:
            source = LiveScoreSource(
                ws_url=ws_url,
                get_frame=lambda: _FRAME,
                reconnect_delay=0.05,
                max_reconnect_delay=0.2,
                connect_timeout=1.0,
                response_timeout=1.0,
            )
            await source.start()
            try:
                await _wait_until(
                    lambda: source.latest().source_status == "model_offline"
                )
                await _wait_until(
                    lambda: source.latest().source_status == "live"
                )
            finally:
                await source.stop()

    asyncio.run(scenario())


def test_hung_handshake_reports_model_offline() -> None:
    async def scenario() -> None:
        async def silent(ws: websockets.ServerConnection) -> None:
            # Never send the ready message; wait for the client to give up.
            await ws.wait_closed()

        async with _fake_model(silent) as ws_url:
            source = LiveScoreSource(
                ws_url=ws_url,
                get_frame=lambda: _FRAME,
                reconnect_delay=0.05,
                connect_timeout=0.5,
                response_timeout=0.3,
            )
            await source.start()
            try:
                await _wait_until(
                    lambda: source.latest().source_status == "model_offline"
                )
            finally:
                await source.stop()

    asyncio.run(scenario())
