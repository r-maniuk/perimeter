"""Sign-in, sign-out, the current user and the sign-in rate limit."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.api.ratelimit import RateLimiter
from perimeter.api.security import RevocationList
from perimeter.api.state import AppState
from tests.support import eventually


async def sign_in(client: httpx.AsyncClient, username: str = "alice") -> dict[str, Any]:
    """A browser sign-in: the session cookie, and a body with the user only."""
    response = await client.post("/v1/session", json={"username": username})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def get_token(client: httpx.AsyncClient, username: str = "alice") -> dict[str, Any]:
    """A command-line sign-in: a bearer token, and no cookie."""
    response = await client.post("/v1/token", json={"username": username})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


async def test_sign_in_creates_the_user_once_and_sets_a_hardened_cookie(
    client: httpx.AsyncClient, api: FastAPI
) -> None:
    response = await client.post("/v1/session", json={"username": "  Alice.Smith "})
    assert response.status_code == 201
    session = response.json()
    assert set(session) == {"expires_at", "user"}  # the token is in the cookie only
    assert session["user"]["username"] == "alice.smith"
    cookie = response.headers["set-cookie"]
    ttl = api.state.perimeter.tokens.ttl_s
    for attribute in ("perimeter_session=", "HttpOnly", "Path=/", f"Max-Age={ttl}"):
        assert attribute in cookie
    assert "samesite=lax" in cookie.lower()
    assert "secure" not in cookie.lower()  # SECURE_COOKIES is off in tests
    first_cookie = client.cookies["perimeter_session"]
    again = await sign_in(client, "ALICE.SMITH")
    assert again["user"] == session["user"]
    assert client.cookies["perimeter_session"] != first_cookie


async def test_a_token_for_a_client_without_cookies_sets_none(client: httpx.AsyncClient) -> None:
    grant = await get_token(client, "Alice")
    assert grant["token_type"] == "bearer"
    assert grant["user"]["username"] == "alice"
    assert not client.cookies
    assert (await client.get("/v1/me", headers=bearer(grant))).json()["username"] == "alice"
    assert (await get_token(client))["token"] != grant["token"]


async def test_cookie_and_bearer_both_identify_the_user(client: httpx.AsyncClient) -> None:
    session = await sign_in(client)
    by_cookie = await client.get("/v1/me")
    assert by_cookie.status_code == 200
    assert by_cookie.json()["id"] == session["user"]["id"]
    client.cookies.clear()
    anonymous = await client.get("/v1/me")
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert anonymous.headers["content-type"] == "application/problem+json"
    grant = await get_token(client)
    by_bearer = (await client.get("/v1/me", headers=bearer(grant))).json()
    assert by_bearer["username"] == "alice"
    assert by_bearer["session_expires_at"] == grant["expires_at"]
    garbage = await client.get("/v1/me", headers={"Authorization": "Bearer not-a-token"})
    assert garbage.status_code == 401


async def test_sign_out_revokes_the_token_on_every_replica(
    client: httpx.AsyncClient, api: FastAPI
) -> None:
    session = await get_token(client)
    state: AppState = api.state.perimeter
    token_id = state.tokens.verify(session["token"]).token_id
    other_replica = await RevocationList.open(state.js, keep_s=60)
    try:
        response = await client.delete("/v1/session", headers=bearer(session))
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]  # a browser's cookie goes too
        assert (await client.get("/v1/me", headers=bearer(session))).status_code == 401
        await eventually(lambda: other_replica.is_revoked(token_id))
    finally:
        await other_replica.close()
    client.cookies.clear()
    assert (await client.delete("/v1/session", headers=bearer(session))).status_code == 204
    assert (await client.delete("/v1/session")).status_code == 204  # nothing to sign out


async def test_a_revoked_cookie_is_refused_too(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    token = client.cookies["perimeter_session"]
    assert (await client.delete("/v1/session")).status_code == 204
    client.cookies.set("perimeter_session", token)
    assert (await client.get("/v1/me")).status_code == 401


@pytest.mark.parametrize(
    "username",
    ["a", "-alice", "al ice", "x" * 33, "alice!", "", "émile", "alice/../../root", None, 42],
)
async def test_invalid_usernames_are_rejected(client: httpx.AsyncClient, username: object) -> None:
    response = await client.post("/v1/session", json={"username": username})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


async def test_unknown_fields_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/session", json={"username": "alice", "password": "x"})
    assert response.status_code == 422


async def test_concurrent_first_sign_ins_create_exactly_one_user(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    responses = await asyncio.gather(
        *(client.post("/v1/session", json={"username": "race"}) for _ in range(12))
    )
    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["user"]["id"] for r in responses}) == 1
    async with db.connect() as conn:
        count: int = (
            await conn.execute(text("SELECT count(*) FROM users WHERE username = 'race'"))
        ).scalar_one()
    assert count == 1


async def test_sign_in_is_rate_limited_per_address(client: httpx.AsyncClient, api: FastAPI) -> None:
    api.state.login_limiter = RateLimiter(rate_per_minute=2)
    await sign_in(client, "one")
    await sign_in(client, "two")
    refused = await client.post("/v1/session", json={"username": "three"})
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate_limited"
    assert 1 <= int(refused.headers["retry-after"]) <= 30
    other_address = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api, client=("198.51.100.9", 4000)),
        base_url="http://testserver",
    )
    async with other_address:
        assert (await other_address.post("/v1/session", json={"username": "four"})).is_success


async def test_a_token_that_outlived_its_account_is_refused(
    client: httpx.AsyncClient, db: AsyncEngine
) -> None:
    session = await get_token(client, "ghost")
    async with db.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE username = 'ghost'"))
    me = await client.get("/v1/me", headers=bearer(session))
    assert me.status_code == 401
    assert "no longer exists" in me.json()["detail"]
