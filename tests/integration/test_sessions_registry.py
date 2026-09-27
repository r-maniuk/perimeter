"""Live sessions across replicas: lists, remote sign-out, the per-user cap, refused sockets."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import msgspec
import pytest
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.exceptions import ConnectionClosed

from perimeter.api.live.sessions import SessionRecord, SessionRegistry
from perimeter.bus import topology
from perimeter.config import Settings
from perimeter.wire import subjects
from tests.integration.conftest import TEST_ORIGIN
from tests.integration.live_support import (
    LiveClient,
    LiveTarget,
    Replica,
    create_user,
    live_client,
    replicas,
    sign_in,
    with_live,
)
from tests.support import eventually


@pytest.fixture
async def cluster(
    settings: Settings,
    provisioned: topology.Topology,
    db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[Replica]]:
    async with replicas(settings, monkeypatch, "api-a", "api-b") as started:
        yield started


@pytest.fixture
async def capped(
    settings: Settings,
    provisioned: topology.Topology,
    db: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[Replica]]:
    capped_settings = with_live(settings, max_sessions_per_user=2)
    async with replicas(capped_settings, monkeypatch, "api-a", "api-b") as started:
        yield started


def by_sid(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["sid"]: entry for entry in frame["sessions"]}


def listing(count: int) -> Any:
    return lambda frame: len(frame["sessions"]) == count


async def rest(replica: Replica, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(base_url=replica.http, headers=headers, timeout=10)


async def test_session_lists_follow_joins_and_leaves_on_every_replica(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    laptop = sign_in(settings, alice, "alice").token
    phone = sign_in(settings, alice, "alice").token
    async with live_client(cluster[0].ws(laptop)) as first:
        me = (await first.expect("hello"))["session_id"]
        await first.expect("sessions", where=listing(1))
        async with (
            live_client(cluster[1].ws(phone)) as second,
            live_client(cluster[1].ws(laptop)) as third,
        ):
            other = (await second.expect("hello"))["session_id"]
            tab = (await third.expect("hello"))["session_id"]
            seen_by_first = by_sid(await first.expect("sessions", where=listing(3)))
            seen_by_second = by_sid(await second.expect("sessions", where=listing(3)))
            current = {sid: entry["current"] for sid, entry in seen_by_first.items()}
            assert current == {me: True, other: False, tab: True}  # same sign-in: laptop
            current = {sid: entry["current"] for sid, entry in seen_by_second.items()}
            assert current == {me: False, other: True, tab: False}
            where = {sid: entry["replica"] for sid, entry in seen_by_first.items()}
            assert where == {me: "api-a", other: "api-b", tab: "api-b"}
        [left] = (await first.expect("sessions", where=listing(1)))["sessions"]
        assert left["sid"] == me


async def test_rest_lists_the_users_sessions_and_marks_those_of_this_sign_in(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    laptop = sign_in(settings, alice, "alice").token
    phone = sign_in(settings, alice, "alice").token
    async with (
        live_client(cluster[0].ws(laptop)) as on_laptop,
        live_client(cluster[1].ws(phone)) as on_phone,
        await rest(cluster[1], laptop) as http,
        await rest(cluster[0], None) as anonymous,
    ):
        laptop_sid = (await on_laptop.expect("hello"))["session_id"]
        phone_sid = (await on_phone.expect("hello"))["session_id"]
        await on_laptop.expect("sessions", where=listing(2))
        body = (await http.get("/v1/sessions")).json()
        listed = by_sid(body)
        assert set(listed) == {laptop_sid, phone_sid}
        assert listed[laptop_sid]["current"] is True
        assert listed[phone_sid]["current"] is False
        assert listed[phone_sid]["label"] == "Python websockets"
        assert listed[phone_sid]["ip"] == "127.0.0.1"
        refused = await anonymous.get("/v1/sessions")
        assert refused.status_code == 401
        assert refused.headers["content-type"] == "application/problem+json"


async def test_remote_sign_out_closes_that_socket_with_4001_and_revokes_its_token(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    laptop = sign_in(settings, alice, "alice").token
    phone = sign_in(settings, alice, "alice").token
    async with (
        live_client(cluster[0].ws(laptop)) as on_laptop,
        live_client(cluster[1].ws(phone)) as on_phone,
        await rest(cluster[0], laptop) as laptop_http,
        await rest(cluster[1], phone) as phone_http,
    ):
        await on_laptop.expect("hello")
        phone_sid = (await on_phone.expect("hello"))["session_id"]
        response = await laptop_http.delete(f"/v1/sessions/{phone_sid}")
        assert response.status_code == 204, response.text
        assert await on_phone.closed() == 4001
        assert (await phone_http.get("/v1/sessions")).status_code == 401
        remaining = await on_laptop.expect("sessions", where=listing(1))
        assert [s["sid"] for s in remaining["sessions"]] != [phone_sid]
        assert (await laptop_http.get("/v1/sessions")).status_code == 200
        async with live_client(cluster[0].ws(phone)) as again:
            assert await again.closed() == 4003


async def test_every_socket_opened_with_a_signed_out_token_closes(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    token = sign_in(settings, alice, "alice").token
    async with (
        live_client(cluster[0].ws(token)) as first,
        live_client(cluster[1].ws(token)) as second,
        await rest(cluster[1], token) as http,
    ):
        sid = (await first.expect("hello"))["session_id"]
        await second.expect("hello")
        assert (await http.delete(f"/v1/sessions/{sid}")).status_code == 204
        codes = await asyncio.gather(first.closed(), second.closed())
        assert list(codes) == [4001, 4001]


async def test_other_users_sessions_cannot_be_signed_out(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    bob = await create_user(db, "bob")
    alice_token = sign_in(settings, alice, "alice").token
    bob_token = sign_in(settings, bob, "bob").token
    async with (
        live_client(cluster[0].ws(alice_token)) as client,
        await rest(cluster[1], bob_token) as bob_http,
    ):
        sid = (await client.expect("hello"))["session_id"]
        response = await bob_http.delete(f"/v1/sessions/{sid}")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"
        assert (await bob_http.delete("/v1/sessions/not-a-session")).status_code == 422
        assert (await bob_http.delete(f"/v1/sessions/{uuid.uuid4()}")).status_code == 404
        await client.send(type="ping", t=1)
        await client.expect("pong")


async def test_the_per_user_cap_holds_across_replicas(
    capped: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    bob = await create_user(db, "bob")
    token = sign_in(settings, alice, "alice").token
    async with (
        live_client(capped[0].ws(token)) as first,
        live_client(capped[1].ws(token)) as second,
    ):
        await first.expect("hello")
        await second.expect("hello")
        async with live_client(capped[0].ws(token)) as third:
            assert await third.closed() == 4009
        async with live_client(capped[1].ws(sign_in(settings, bob, "bob").token)) as other_user:
            await other_user.expect("hello")
        await second.close()
        await first.expect("sessions", where=listing(1))
        async with live_client(capped[1].ws(token)) as replacement:
            await replacement.expect("hello")


async def test_concurrent_connections_never_exceed_the_cap(
    capped: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    token = sign_in(settings, alice, "alice").token

    async def attempt(replica: Replica) -> tuple[LiveClient, int | None]:
        client = await LiveClient.open(replica.ws(token))
        try:
            first = await client.ws.recv()
        except ConnectionClosed as exc:
            return client, exc.rcvd.code if exc.rcvd is not None else -1
        assert '"hello"' in str(first)
        return client, None

    outcomes = await asyncio.gather(*(attempt(capped[n % 2]) for n in range(6)))
    try:
        codes = sorted(str(code) for _, code in outcomes)
        assert codes == ["4009", "4009", "4009", "4009", "None", "None"]
    finally:
        await asyncio.gather(*(client.close() for client, _ in outcomes))


async def test_sockets_without_valid_credentials_are_refused_with_4003(
    cluster: list[Replica], db: AsyncEngine, settings: Settings
) -> None:
    alice = await create_user(db, "alice")
    token = sign_in(settings, alice, "alice").token
    forged = jwt.encode(
        {"sub": str(alice), "name": "alice", "jti": "x", "iat": 1, "exp": 4_102_444_800},
        "another-secret-that-is-long-enough-to-sign-0123456789",
        algorithm="HS256",
    )
    replica = cluster[0]
    refused: list[tuple[LiveTarget, dict[str, str]]] = [
        (replica.ws(), {}),  # no credentials at all
        (LiveTarget(f"{replica.ws().url}?token={token}"), {}),  # tokens in URLs do not count
        (replica.ws("garbage"), {}),
        (replica.ws(forged), {}),  # signed with another key
        (replica.ws(), {"cookie": token, "origin": "https://evil.example"}),  # cross-site
        (replica.ws(), {"cookie": token}),  # a cookie without an Origin: not a browser
        (replica.ws(token), {"origin": "https://evil.example"}),  # a page elsewhere, even with it
    ]
    for url, options in refused:
        async with live_client(url, **options) as client:
            assert await client.closed() == 4003, (url, options)
    async with live_client(replica.ws(), cookie=token, origin=TEST_ORIGIN) as browser:
        hello = await browser.expect("hello")
        assert hello["user"]["username"] == "alice"
    # A command-line client that also kept the cookie: its explicit token decides.
    async with live_client(replica.ws(token), cookie=token) as tool:
        assert (await tool.expect("hello"))["user"]["username"] == "alice"


async def test_a_recreated_bucket_is_followed_from_its_first_revision(
    nc: NatsClient, js: JetStreamContext, provisioned: topology.Topology
) -> None:
    def record(uid: str, sid: str) -> bytes:
        return msgspec.json.encode(
            SessionRecord(
                sid=sid,
                uid=uid,
                replica="elsewhere",
                connected_at=datetime.now(UTC),
                agent=None,
                label="Firefox · Linux",
                ip=None,
                jti=f"jti-{sid}",
            )
        )

    registry = SessionRegistry(js, nc, instance="replica-a", max_per_user=16)
    await registry.start()
    try:
        kv = await js.key_value(subjects.KV_SESSIONS)
        for index in range(5):
            await kv.put(f"u1.s{index}", record("u1", f"s{index}"))
        await eventually(lambda: len(registry.sessions_of("u1")) == 5)

        # The bucket is lost and recreated: its revisions start again from 1, below the mirror's.
        await js.delete_key_value(subjects.KV_SESSIONS)
        await topology.ensure(js, provisioned)
        kv = await js.key_value(subjects.KV_SESSIONS)
        await kv.put("u2.s9", record("u2", "s9"))

        await registry._follow_a_recreated_bucket()
        await eventually(lambda: [s.sid for s in registry.sessions_of("u2")] == ["s9"])
    finally:
        await registry.close()
