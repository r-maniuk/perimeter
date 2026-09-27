"""The broker's permission matrix (``infra/nats/nats.conf``) against a real NATS server.

The server runs the repository's configuration with password hashes rendered by
:mod:`perimeter.tools.secrets`, exactly as in the compose stack. Each service user performs what
its process does, through the same foundation modules, and must not trigger a single violation;
the operations the matrix exists to prevent must be refused and logged by the server.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import nats
import pytest
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig, DeliverPolicy, StreamConfig
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from perimeter.api.security import RevocationList
from perimeter.bus import topology
from perimeter.bus.connection import connect
from perimeter.bus.leases import LeaseBucket, generation_of
from perimeter.bus.relay import OutboxRelay
from perimeter.config import NatsSettings
from perimeter.domain import tiles
from perimeter.ops.heartbeat import Heartbeat
from perimeter.storage import outbox
from perimeter.storage.outbox import PendingEvent
from perimeter.tools.secrets import NATS_USERS, NatsPasswords, nats_password_secret
from perimeter.wire import subjects
from tests.integration.conftest import NATS_IMAGE
from tests.support import eventually

CONFIG = Path(__file__).resolve().parents[2] / "infra" / "nats" / "nats.conf"
TOPOLOGY = topology.Topology(partitions=4, lease_ttl_s=2, sessions_ttl_s=5, revoked_ttl_s=60)


@dataclass(frozen=True, slots=True)
class SecuredBroker:
    url: str
    passwords: dict[str, str]
    container: DockerContainer

    def settings(self, user: str) -> NatsSettings:
        return NatsSettings(url=self.url, user=user, password=SecretStr(self.passwords[user]))

    def violations(self) -> list[str]:
        stdout, stderr = self.container.get_logs()
        lines = (stdout + stderr).decode(errors="replace").splitlines()
        return [line for line in lines if "Violation" in line]


@pytest.fixture(scope="module")
def broker() -> Iterator[SecuredBroker]:
    passwords = {user: secrets.token_urlsafe(32) for user in NATS_USERS}
    rendered = NatsPasswords(NATS_USERS).render(
        {nats_password_secret(user): password for user, password in passwords.items()}
    )
    container = (
        DockerContainer(NATS_IMAGE)
        .with_command("--config /etc/nats/nats.conf")
        .with_copy_into_container(CONFIG.read_bytes(), "/etc/nats/nats.conf")
        .with_copy_into_container(rendered, "/etc/nats/auth/passwords.conf")
        .with_exposed_ports(4222)
        .waiting_for(LogMessageWaitStrategy("Server is ready"))
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(4222)
        yield SecuredBroker(f"nats://{host}:{port}", passwords, container)


@pytest.fixture(scope="module")
async def provisioned_broker(broker: SecuredBroker) -> SecuredBroker:
    """The init job's work, done as the ``init`` user (twice: provisioning is idempotent)."""
    nc = await connect(broker.settings("init"), name="test-init")
    try:
        await topology.ensure(nc.jetstream(), TOPOLOGY)
        await topology.ensure(nc.jetstream(), TOPOLOGY)
    finally:
        await nc.drain()
    assert broker.violations() == []
    return broker


@pytest.fixture
async def service(
    provisioned_broker: SecuredBroker, request: pytest.FixtureRequest
) -> AsyncIterator[NatsClient]:
    """A client connected like the service named by the test's ``user`` parameter."""
    user: str = request.param
    nc = await connect(provisioned_broker.settings(user), name=f"test-{user}")
    yield nc
    await nc.drain()


async def _relay_through(db: AsyncEngine, js: JetStreamContext, subject: str) -> int:
    async with db.begin() as conn:
        rows = await outbox.insert(
            conn, [PendingEvent(subject, f"test-{secrets.token_hex(8)}", b'{"n":1}')]
        )
    return await OutboxRelay(db, js).relay(rows)


async def _publish_reports(broker: SecuredBroker, *, devices: int) -> None:
    """Device reports enter TELEMETRY only through the api, so seed them as the api."""
    nc = await connect(broker.settings("api"), name="test-api-ingest")
    try:
        js = nc.jetstream()
        for n in range(devices):
            device = f"veh-{secrets.token_hex(3)}-{n}"
            await js.publish(subjects.telemetry(device), b"r", headers={"Nats-Msg-Id": device})
    finally:
        await nc.drain()


async def _direct_batch(nc: NatsClient, stream: str, request: dict[str, object]) -> list[str]:
    """Subjects returned by a batched direct get: a stateless read served by the stream itself."""
    inbox = nc.new_inbox()
    replies = await nc.subscribe(inbox)
    await nc.publish(f"$JS.API.DIRECT.GET.{stream}", json.dumps(request).encode(), reply=inbox)
    found: list[str] = []
    while "Status" not in (headers := (await replies.next_msg(timeout=2)).headers or {}):
        found.append(headers["Nats-Subject"])  # the batch ends with a status message
    await replies.unsubscribe()
    return found


async def _one_heartbeat(nc: NatsClient, service: str) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        Heartbeat(nc, service=service, instance="test", snapshot=dict, interval_s=0.05).run(stop)
    )
    await asyncio.sleep(0.1)
    stop.set()
    await task


@pytest.mark.parametrize("service", ["api"], indirect=True)
async def test_the_api_can_do_everything_it_needs(
    service: NatsClient, provisioned_broker: SecuredBroker, db: AsyncEngine
) -> None:
    nc, js = service, service.jetstream()
    await topology.verify(js, TOPOLOGY)

    received: list[Msg] = []

    async def collect(msg: Msg) -> None:
        received.append(msg)

    for subject in (
        subjects.live_of_user("u1"),  # exactly the api's per-user feed subscription
        "ctl.ses.*",
        "pos.>",
        "sys.metrics.>",
    ):
        await nc.subscribe(subject, cb=collect)

    # ingest: acknowledged publishes, de-duplicated by id
    ack = await (
        await js.publish_async(
            subjects.telemetry("dev-1"), b"r", headers={"Nats-Msg-Id": "dev-1:1"}
        )
    )
    assert ack.stream == subjects.TELEMETRY_STREAM
    assert len(await js.consumers_info(subjects.TELEMETRY_STREAM)) == TOPOLOGY.partitions

    # zone events through the outbox relay, republished for live delivery
    assert await _relay_through(db, js, subjects.events("u1")) == 1
    await eventually(lambda: any(m.subject == "live.evt.u1" for m in received))
    info = await js.stream_info(subjects.EVENTS_STREAM, subjects_filter=subjects.events("u1"))
    assert info.state.messages >= 1
    stored = await js.get_msg(subjects.EVENTS_STREAM, seq=info.state.first_seq)
    assert stored.subject == subjects.events("u1")

    # resume replay, gap healing and device trail: batched direct gets
    replayed = await _direct_batch(
        nc,
        subjects.EVENTS_STREAM,
        {"seq": info.state.first_seq, "next_by_subj": subjects.events("u1"), "batch": 16},
    )
    assert replayed == [subjects.events("u1")]
    last = await js.get_last_msg(subjects.EVENTS_STREAM, subjects.events("u1"), direct=True)
    assert last.subject == subjects.events("u1")
    trail = await _direct_batch(
        nc,
        subjects.TELEMETRY_STREAM,
        {"seq": 1, "next_by_subj": subjects.telemetry_of_device("dev-1"), "batch": 16},
    )
    assert [subjects.device_of(subject) for subject in trail] == ["dev-1"]

    # ... or ephemeral ordered consumers
    replay = await js.subscribe(
        subjects.events("u1"),
        stream=subjects.EVENTS_STREAM,
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
        config=ConsumerConfig(opt_start_seq=info.state.first_seq),
    )
    assert (await replay.next_msg(timeout=2)).subject == subjects.events("u1")
    await replay.unsubscribe()
    ordered_trail = await js.subscribe(
        subjects.telemetry_of_device("dev-1"),
        stream=subjects.TELEMETRY_STREAM,
        ordered_consumer=True,
    )
    assert subjects.device_of((await ordered_trail.next_msg(timeout=2)).subject) == "dev-1"
    await ordered_trail.unsubscribe()

    # sessions registry and remote sign-out
    sessions = await js.key_value(subjects.KV_SESSIONS)
    await sessions.put("u1.s1", b"{}")
    assert (await sessions.get("u1.s1")).value == b"{}"
    assert await sessions.keys() == ["u1.s1"]
    await sessions.delete("u1.s1")
    revocations = await RevocationList.open(js)
    await revocations.revoke("jti-1")
    await revocations.close()

    # core notifications and the heartbeat
    await nc.publish(subjects.live_sessions("u1"), b"{}")
    await nc.publish(subjects.session_control("s1"), b"{}")
    await _one_heartbeat(nc, "api")
    await eventually(lambda: any(m.subject.startswith("sys.metrics.api.") for m in received))
    assert provisioned_broker.violations() == []


@pytest.mark.parametrize("service", ["engine"], indirect=True)
async def test_the_engine_can_do_everything_it_needs(
    service: NatsClient, provisioned_broker: SecuredBroker, db: AsyncEngine
) -> None:
    nc, js = service, service.jetstream()
    await topology.verify(js, TOPOLOGY)

    # membership and partition leases
    info = await js.stream_info(f"KV_{subjects.KV_ENGINE}")
    leases = LeaseBucket(
        await js.key_value(subjects.KV_ENGINE), generation=generation_of(info.created)
    )
    await leases.heartbeat("m.engine-test", b"{}")
    lease = await leases.acquire("p.0", "engine-test")
    assert lease is not None
    lease = await leases.renew(lease)
    assert await leases.holder("p.0") == "engine-test"
    assert await leases.keys("m.") == ["m.engine-test"]
    await leases.release(lease)

    # pull every partition: fetch, then ack / nak / term
    await _publish_reports(provisioned_broker, devices=12)
    first_report = await js.get_msg(subjects.TELEMETRY_STREAM, 1, direct=True)
    assert first_report.subject is not None
    fetched = 0
    for partition in range(TOPOLOGY.partitions):
        pull = await js.pull_subscribe_bind(
            subjects.engine_consumer(partition), stream=subjects.TELEMETRY_STREAM
        )
        try:
            batch = await pull.fetch(10, timeout=0.5)
        except nats.errors.TimeoutError:
            batch = []
        for index, msg in enumerate(batch):
            settle = (msg.ack, msg.term, msg.in_progress)[index % 3]
            await settle()
        fetched += len(batch)
        await pull.unsubscribe()
    assert fetched >= 12

    # taking a partition over: reread what the previous owner fetched but never acknowledged
    partition = subjects.partition_of(first_report.subject)
    reread = await js.get_msg(
        subjects.TELEMETRY_STREAM,
        1,
        subject=subjects.telemetry_partition(partition),
        direct=True,
        next=True,
    )
    assert reread.subject is not None
    assert subjects.partition_of(reread.subject) == partition

    # alerts through the outbox (fast path and sweeper), positions, pulses, heartbeat
    assert await _relay_through(db, js, subjects.events("u2")) == 1
    await nc.publish(tiles.subject_for(tiles.quadkey_for(4.9041, 52.3676, 12)), b"\xb7")
    await nc.publish(subjects.live_pulses("u2"), b"{}")
    await _one_heartbeat(nc, "engine")
    await nc.flush()
    assert provisioned_broker.violations() == []


Attempt = Callable[[NatsClient, JetStreamContext], Awaitable[object]]


def _durable_takeover(nc: NatsClient, js: JetStreamContext) -> Awaitable[object]:
    config = ConsumerConfig(name="engine-p0", durable_name="engine-p0", filter_subject="tlm.0.*")
    return js.add_consumer(subjects.TELEMETRY_STREAM, config)


async def _pull_engine_partition(nc: NatsClient, js: JetStreamContext) -> object:
    pull = await js.pull_subscribe_bind("engine-p0", stream=subjects.TELEMETRY_STREAM)
    return await pull.fetch(1, timeout=0.5)


def _rogue_stream(nc: NatsClient, js: JetStreamContext) -> Awaitable[object]:
    return js.add_stream(StreamConfig(name="ROGUE", subjects=["rogue.>"]))


FORBIDDEN: list[tuple[str, str, Attempt]] = [
    ("api", "$JS.API.STREAM.DELETE.TELEMETRY", lambda nc, js: js.delete_stream("TELEMETRY")),
    ("api", "$JS.API.STREAM.PURGE.EVENTS", lambda nc, js: js.purge_stream("EVENTS")),
    ("api", "$JS.API.CONSUMER.CREATE.TELEMETRY.engine-p0", _durable_takeover),
    ("api", "$JS.API.CONSUMER.MSG.NEXT.TELEMETRY.engine-p0", _pull_engine_partition),
    (
        "api",
        "$JS.API.CONSUMER.DELETE.TELEMETRY.engine-p0",
        lambda nc, js: js.delete_consumer("TELEMETRY", "engine-p0"),
    ),
    ("api", "$KV.engine.p.0", lambda nc, js: js.publish("$KV.engine.p.0", b"api")),
    ("api", "_INBOX.engine.>", lambda nc, js: nc.subscribe("_INBOX.engine.>")),
    ("engine", "tlm.dev-9", lambda nc, js: js.publish("tlm.dev-9", b"r")),
    ("engine", "$KV.sessions.u1.s1", lambda nc, js: js.publish("$KV.sessions.u1.s1", b"{}")),
    ("engine", "$JS.API.STREAM.CREATE.ROGUE", _rogue_stream),
    ("engine", "sys.metrics.>", lambda nc, js: nc.subscribe("sys.metrics.>")),
    ("init", "$JS.API.STREAM.DELETE.EVENTS", lambda nc, js: js.delete_stream("EVENTS")),
    (
        "init",
        "$JS.API.CONSUMER.DELETE.TELEMETRY.engine-p0",
        lambda nc, js: js.delete_consumer("TELEMETRY", "engine-p0"),
    ),
    ("init", "evt.u1", lambda nc, js: js.publish("evt.u1", b"{}")),
]


@pytest.mark.parametrize(
    ("user", "subject", "attempt"), FORBIDDEN, ids=[f"{u}:{s}" for u, s, _ in FORBIDDEN]
)
async def test_what_a_service_must_not_do_is_refused(
    provisioned_broker: SecuredBroker, user: str, subject: str, attempt: Attempt
) -> None:
    errors: list[Exception] = []

    async def on_error(exc: Exception) -> None:
        errors.append(exc)

    nc = await nats.connect(
        provisioned_broker.url,
        user=user,
        password=provisioned_broker.passwords[user],
        inbox_prefix=subjects.inbox_prefix(user),
        error_cb=on_error,
        allow_reconnect=False,
    )
    try:
        with suppress(nats.errors.TimeoutError):  # a refused request is never answered
            await attempt(nc, nc.jetstream(timeout=0.5))
        await eventually(lambda: errors)
    finally:
        await nc.close()
    assert "permissions violation" in str(errors[0]).lower()
    assert any(f'Subject "{subject}' in line for line in provisioned_broker.violations())

    checker = await connect(provisioned_broker.settings("api"), name="test-checker")
    try:
        await topology.verify(checker.jetstream(), TOPOLOGY)  # nothing was deleted or replaced
    finally:
        await checker.drain()


async def _handshake(url: str, credentials: dict[str, str]) -> str:
    """The server's answer to a bare protocol CONNECT (no client library in between)."""
    address = urlparse(url)
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    try:
        await reader.readline()  # INFO
        options = json.dumps({"verbose": False, "pedantic": False, **credentials})
        writer.write(f"CONNECT {options}\r\nPING\r\n".encode())
        await writer.drain()
        return (await asyncio.wait_for(reader.readline(), 5)).decode().strip()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize(
    "credentials",
    [{}, {"user": "api", "pass": "not-the-password"}, {"user": "nobody", "pass": "x"}],
    ids=["anonymous", "wrong-password", "unknown-user"],
)
async def test_every_connection_must_authenticate(
    provisioned_broker: SecuredBroker, credentials: dict[str, str]
) -> None:
    answer = await _handshake(provisioned_broker.url, credentials)
    assert answer == "-ERR 'Authorization Violation'"
    valid = {"user": "engine", "pass": provisioned_broker.passwords["engine"]}
    assert await _handshake(provisioned_broker.url, valid) == "PONG"
