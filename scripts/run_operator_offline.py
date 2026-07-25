"""Safe visual/contract preview of the operator workbench without DimOS.

No route in this process publishes robot commands. Mode changes update only the
mock status so browser QA can verify the three-mode UI and epoch protocol.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
HTML = (
    (ROOT / "nightwatch/nightwatch/operator_console.html")
    .read_text(encoding="utf-8")
    .replace(
        "● 正在连接机器人",
        "● OFFLINE SAFE PREVIEW · NO HARDWARE COMMANDS",
    )
    .replace("__NIGHTWATCH_FORM_URL__", "http://localhost:3000/form")
)
GUIDE = (ROOT / "nightwatch/nightwatch/operator_guide.html").read_text(
    encoding="utf-8"
)
CAMERA = """<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">
<rect width="100%" height="100%" fill="#050809"/>
<text x="50%" y="48%" text-anchor="middle" fill="#f2e84b"
font-family="monospace" font-size="42">ROBOT CAMERA OFFLINE</text>
<text x="50%" y="56%" text-anchor="middle" fill="#9fe2c2"
font-family="monospace" font-size="24">safe workbench preview</text></svg>"""

app = FastAPI(title="Nightwatch offline workbench")
state = {
    "operating_mode": "autonomous",
    "mode_epoch": 0,
    "manual_input_seq": 0,
    "scan_interval_s": 25,
    "scan_duration_s": 7,
}


@app.get("/operator", response_class=HTMLResponse)
async def operator() -> HTMLResponse:
    return HTMLResponse(HTML)


@app.get("/operator/help", response_class=HTMLResponse)
async def guide() -> HTMLResponse:
    return HTMLResponse(GUIDE)


@app.get("/video_feed/camera")
async def camera() -> Response:
    return Response(CAMERA, media_type="image/svg+xml")


@app.get("/operator/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "success": True,
            "robot_connected": False,
            **state,
            "behavior": "offline_preview",
            "owner": "none",
            "interaction_state": "idle",
            "fatigue_detection": "disabled",
            "fatigue_pause_reason": "robot_offline",
            "battery_soc": None,
            "map_phase": "offline",
            "navigation": "offline",
            "camera_age_ms": None,
            "hold_reason": "OFFLINE_PREVIEW",
        }
    )


@app.post("/operator/action")
async def action(request: Request) -> JSONResponse:
    body = await request.json()
    action_name = str(body.get("action", ""))
    arguments = body.get("arguments") or {}
    if action_name == "operating_mode":
        mode = str(arguments.get("mode", ""))
        if mode not in {"autonomous", "sleep_analysis", "manual"}:
            return JSONResponse(
                status_code=409,
                content={"success": False, "message": "invalid mock mode"},
            )
        if mode != state["operating_mode"]:
            state["operating_mode"] = mode
            state["mode_epoch"] += 1
        return JSONResponse(
            {"success": True, "message": f"Offline preview mode: {mode}"}
        )
    if action_name == "scan_settings":
        state["scan_interval_s"] = max(
            10, min(300, int(arguments.get("interval_s", 25)))
        )
        state["scan_duration_s"] = max(
            3, min(9, int(arguments.get("duration_s", 7)))
        )
        return JSONResponse({"success": True, "message": "Mock settings saved"})
    if action_name == "teleop":
        valid = (
            state["operating_mode"] == "manual"
            and int(arguments.get("epoch", -1)) == state["mode_epoch"]
            and int(arguments.get("sequence", 0)) > state["manual_input_seq"]
        )
        if valid:
            state["manual_input_seq"] = int(arguments["sequence"])
        return JSONResponse(
            status_code=200 if valid else 409,
            content={
                "success": valid,
                "message": "Mock packet validated; no hardware output."
                if valid
                else "Mock manual packet rejected.",
            },
        )
    if action_name == "emergency_stop":
        state["operating_mode"] = "manual"
        state["mode_epoch"] += 1
        return JSONResponse(
            {"success": True, "message": "Mock emergency stop latched manual"}
        )
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "message": f"{action_name or 'action'} blocked: offline preview",
        },
    )


@app.get("/text_stream/agent_responses")
async def responses() -> StreamingResponse:
    async def stream():
        yield "data: Offline preview active; hardware commands are blocked.\n\n"
        while True:
            await asyncio.sleep(15)
            yield ": keep-alive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5555, type=int)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
