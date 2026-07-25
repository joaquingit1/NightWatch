from __future__ import annotations

import asyncio

from app.routers import api

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_qr_png_encodes_env_override_and_caches_bytes(monkeypatch) -> None:
    monkeypatch.setenv(
        "NIGHTWATCH_PUBLIC_FORM_URL", "http://10.0.0.5:3000/form"
    )
    api._QR_PNG_CACHE.clear()

    response = asyncio.run(api.get_intake_qr())

    assert response.media_type == "image/png"
    assert bytes(response.body)[:8] == _PNG_MAGIC
    assert api._QR_PNG_CACHE == {
        "http://10.0.0.5:3000/form": bytes(response.body)
    }

    again = asyncio.run(api.get_intake_qr())
    assert bytes(again.body) == bytes(response.body)


def test_default_form_url_never_points_at_localhost(monkeypatch) -> None:
    monkeypatch.delenv("NIGHTWATCH_PUBLIC_FORM_URL", raising=False)

    url = api._public_form_url()

    assert url.startswith("http://")
    assert url.endswith(":3000/form")
    assert "localhost" not in url
    assert "http://127." not in url
