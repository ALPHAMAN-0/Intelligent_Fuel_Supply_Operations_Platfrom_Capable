"""Operator sign-in: token verification endpoint and the static login page served by the backend."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from fuelops.backend.api.routes import router
from fuelops.backend.main import create_app
from fuelops.shared.config import Settings
from tests.stack import TOKEN

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
VERIFY = "/api/auth/verify"


def _client(operator_token: str | None) -> httpx.AsyncClient:
    """The real operator router on a bare app; `require_operator` only needs `container.settings`."""
    app = FastAPI()
    app.state.container = SimpleNamespace(settings=Settings(operator_token=operator_token))
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend")


async def test_verify_accepts_the_configured_token():
    async with _client(TOKEN) as client:
        r = await client.get(VERIFY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "actor": "operator"}


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": "Bearer "},
    {"Authorization": f"Basic {TOKEN}"},
], ids=["missing", "wrong", "empty", "wrong-scheme"])
async def test_verify_rejects_anything_but_the_configured_bearer_token(headers):
    async with _client(TOKEN) as client:
        r = await client.get(VERIFY, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("configured", [None, ""], ids=["unset", "empty"])
async def test_verify_reports_a_server_without_operator_token(configured):
    async with _client(configured) as client:
        r = await client.get(VERIFY, headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


async def test_verify_rejects_a_non_ascii_token_with_401_not_500():
    # Starlette decodes header bytes as latin-1; hmac.compare_digest raises TypeError on non-ASCII str.
    async with _client(TOKEN) as client:
        r = await client.get(VERIFY, headers={"Authorization": "Bearer café".encode("latin-1")})
    assert r.status_code == 401


async def test_verify_never_echoes_the_token():
    async with _client(TOKEN) as client:
        ok = await client.get(VERIFY, headers={"Authorization": f"Bearer {TOKEN}"})
        bad = await client.get(VERIFY, headers={"Authorization": "Bearer guess-123"})
    assert TOKEN not in ok.text
    assert "guess-123" not in bad.text


async def test_backend_serves_login_page_scripts_and_verify(stack):
    settings = stack.container.settings.model_copy(update={"static_dir": str(FRONTEND_DIR)})
    app = create_app(settings, container=stack.container, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        page = await client.get("/login.html")
        script = await client.get("/js/login.js")
        verified = await client.get(VERIFY, headers={"Authorization": f"Bearer {TOKEN}"})

    assert page.status_code == 200
    assert 'id="login-form"' in page.text
    assert "SIMULATED DATA" in page.text
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert verified.status_code == 200
