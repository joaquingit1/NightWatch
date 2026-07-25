"""Run the NightWatch operator console without DimOS or a physical robot.

The page is loaded directly from ``nightwatch/nightwatch/webchat.py`` so the
offline preview stays aligned with the real operator console. Robot commands
are deliberately refused with HTTP 409; this process must never pretend that
an action reached hardware.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from collections import deque
from pathlib import Path
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
WEBCHAT_SOURCE = ROOT / "nightwatch" / "nightwatch" / "webchat.py"
ASSESSMENTS: deque[dict[str, Any]] = deque(maxlen=50)


def _source_constant(name: str) -> Any:
    tree = ast.parse(WEBCHAT_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"{name} was not found in {WEBCHAT_SOURCE}")


OPERATOR_ACTIONS = set(_source_constant("_OPERATOR_ACTIONS")) | set(
    _source_constant("_DIRECT_OPERATOR_ACTIONS")
)
OPERATOR_HTML = (
    (ROOT / "nightwatch" / "nightwatch" / "operator_console.html")
    .read_text(encoding="utf-8")
    .replace(
        '<span class="live" id="linkState">● 正在连接机器人</span>',
        '<span class="live" id="linkState" style="color:#ffcf6b">● 离线安全预览</span>',
    )
    .replace(
        '<div id="status">工作台已就绪。</div>',
        '<div id="status">离线预览已启动；所有机器人命令都会被拦截。</div>',
    )
    .replace("__NIGHTWATCH_FORM_URL__", "http://localhost:3000/form")
)
OPERATOR_GUIDE_HTML = (
    ROOT / "nightwatch" / "nightwatch" / "operator_guide.html"
).read_text(encoding="utf-8")

CAMERA_PLACEHOLDER = """\
<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720"
     viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#050809"/>
  <g fill="none" stroke="#34505e" stroke-width="8">
    <rect x="420" y="225" width="440" height="270" rx="28"/>
    <circle cx="640" cy="360" r="92"/>
    <path d="M510 225l55-80h150l55 80"/>
  </g>
  <text x="640" y="585" fill="#ffcf6b" font-family="system-ui, sans-serif"
        font-size="36" text-anchor="middle">ROBOT CAMERA OFFLINE</text>
  <text x="640" y="635" fill="#78909b" font-family="system-ui, sans-serif"
        font-size="24" text-anchor="middle">Safe operator-console preview</text>
</svg>
"""

app = FastAPI(title="NightWatch offline operator")


@app.get("/operator", response_class=HTMLResponse)
async def operator_console() -> HTMLResponse:
    return HTMLResponse(OPERATOR_HTML)


@app.get("/operator/help", response_class=HTMLResponse)
async def operator_guide() -> HTMLResponse:
    return HTMLResponse(OPERATOR_GUIDE_HTML)


@app.get("/video_feed/camera")
async def camera_placeholder() -> Response:
    return Response(CAMERA_PLACEHOLDER, media_type="image/svg+xml")


@app.get("/operator/status")
async def operator_status() -> JSONResponse:
    now = time.time()
    recent = []
    for record in reversed(list(ASSESSMENTS)[-5:]):
        item = dict(record)
        item["age_s"] = max(0.0, now - float(item["ts"]))
        recent.append(item)
    return JSONResponse(
        {
            "success": True,
            "robot_connected": False,
            "behavior": "hold",
            "owner": "offline-console",
            "mission_mode": "exploration",
            "control_mode": "autonomous",
            "interaction_state": "idle",
            "fatigue_detection": "disabled",
            "fatigue_pause_reason": "robot_offline",
            "patrol_interval_s": 90,
            "scan_duration_s": 20,
            "map_completion_prompt": None,
            "battery_soc": None,
            "map_phase": "unavailable",
            "navigation": "offline",
            "camera_age_ms": None,
            "areas": 0,
            "stable_objects": 0,
            "remembered_people": 0,
            "hold_reason": "ROBOT_NOT_STARTED",
            "assessments": recent,
            "assessment_count": len(ASSESSMENTS),
        }
    )


@app.post("/operator/action")
async def operator_action(request: Request) -> JSONResponse:
    data = await request.json()
    action = str(data.get("action", ""))
    if action not in OPERATOR_ACTIONS:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": f"Unknown action: {action}"},
        )
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "message": (
                f"{action.replace('_', ' ').title()} blocked: robot is offline."
            ),
        },
    )


@app.post("/operator/assessment")
async def operator_assessment(request: Request) -> JSONResponse:
    try:
        data = await request.json()
        record = {
            "assessment_id": str(data["assessment_id"]),
            "track_id": str(data["track_id"]),
            "fatigue_score": float(data["fatigue_score"]),
            "confidence": float(data["confidence"]),
            "ts": float(data["ts"]),
        }
    except (KeyError, TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid fatigue assessment"},
        )
    ASSESSMENTS.append(record)
    return JSONResponse({"ok": True})


@app.post("/submit_query")
async def submit_query() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "message": "Robot AI is offline; the message was not sent.",
        },
    )


@app.get("/text_stream/agent_responses")
async def agent_responses() -> StreamingResponse:
    async def events():
        yield "data: Offline preview active; robot AI is not connected.\n\n"
        while True:
            await asyncio.sleep(15)
            yield ": keep-alive\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5555, type=int)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
