"""Local OpenAI-compatible proxy that makes the OpenAgents gateway usable.

The gateway's schema requires message `content` to be a string, but every
tool-call round trip contains an assistant message with content null (and
vision requests contain content arrays). The gateway 422s those, which is
what kept killing the agent. This proxy rewrites requests in flight:

- content null            -> ""
- content [parts...]      -> concatenated text parts (images dropped)

and forwards everything else verbatim, streaming included.

Run (own process, alongside the stack):
    python -m nightwatch.llmproxy          # listens on 127.0.0.1:8899

Then: OPENAI_BASE_URL=http://127.0.0.1:8899/v1 with the OpenAgents key.
"""

import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
import uvicorn

UPSTREAM = "https://api-gateway.openagents.org/v1"
PORT = 8899

app = FastAPI()
client = httpx.AsyncClient(timeout=180.0)


def _fix_messages(body: dict) -> dict:
    for msg in body.get("messages", []):
        content = msg.get("content")
        if content is None:
            msg["content"] = ""
        elif isinstance(content, list):
            msg["content"] = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
    return body


def _auth_headers(request: Request) -> dict:
    auth = request.headers.get("authorization")
    if not auth and os.getenv("OPENAGENTS_API_KEY"):
        auth = f"Bearer {os.environ['OPENAGENTS_API_KEY']}"
    return {"Authorization": auth or "", "Content-Type": "application/json"}


@app.get("/v1/models")
async def models(request: Request) -> Response:
    r = await client.get(f"{UPSTREAM}/models", headers=_auth_headers(request))
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    body = _fix_messages(await request.json())
    headers = _auth_headers(request)

    if body.get("stream"):
        upstream = client.stream(
            "POST", f"{UPSTREAM}/chat/completions", json=body, headers=headers
        )

        async def relay():
            async with upstream as r:
                async for chunk in r.aiter_bytes():
                    yield chunk

        return StreamingResponse(relay(), media_type="text/event-stream")

    r = await client.post(f"{UPSTREAM}/chat/completions", json=body, headers=headers)
    try:
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except json.JSONDecodeError:
        return Response(content=r.content, status_code=r.status_code)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
