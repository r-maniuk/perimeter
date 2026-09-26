"""Idempotent provisioning of streams, consumers and key-value buckets.

The ``init`` job applies this once per deployment; services call :func:`verify` on start-up and
refuse to run against a topology that does not match their configuration (most importantly the
partition count, which decides where every device's reports land).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    DiscardPolicy,
    KeyValueConfig,
    RePublish,
    RetentionPolicy,
    StorageType,
    StreamConfig,
    SubjectTransform,
)
from nats.js.errors import BucketNotFoundError, NotFoundError

from perimeter.config import Settings
from perimeter.wire import subjects

log = structlog.get_logger(__name__)


class TopologyError(RuntimeError):
    """The broker's configuration does not match what this deployment expects."""


@dataclass(frozen=True, slots=True)
class Topology:
    partitions: int = 16
    telemetry_max_age_s: float = 7_200
    telemetry_max_bytes: int = 2 * 1024**3
    telemetry_dedup_s: float = 30
    events_max_age_s: float = 86_400
    events_dedup_s: float = 300
    lease_ttl_s: float = 6
    sessions_ttl_s: float = 30
    revoked_ttl_s: float = 43_200
    consumer_ack_wait_s: float = 30
    consumer_max_ack_pending: int = 20_000

    @classmethod
    def from_settings(cls, settings: Settings) -> Topology:
        return cls(
            partitions=settings.telemetry.partitions,
            telemetry_max_age_s=settings.telemetry.max_age_s,
            telemetry_max_bytes=settings.telemetry.max_bytes,
            telemetry_dedup_s=settings.telemetry.dedup_window_s,
            lease_ttl_s=settings.engine.lease_ttl_s,
            revoked_ttl_s=settings.security.session_ttl_s,
        )


def telemetry_stream(topo: Topology) -> StreamConfig:
    return StreamConfig(
        name=subjects.TELEMETRY_STREAM,
        description="Device location reports, partitioned by device; doubles as the trail log.",
        subjects=[subjects.TELEMETRY_INPUT],
        subject_transform=SubjectTransform(
            src=subjects.TELEMETRY_INPUT,
            dest=subjects.telemetry_partition_transform(topo.partitions),
        ),
        retention=RetentionPolicy.LIMITS,
        discard=DiscardPolicy.OLD,
        storage=StorageType.FILE,
        max_age=topo.telemetry_max_age_s,
        max_bytes=topo.telemetry_max_bytes,
        duplicate_window=topo.telemetry_dedup_s,
        allow_direct=True,
        num_replicas=1,
    )


def events_stream(topo: Topology) -> StreamConfig:
    return StreamConfig(
        name=subjects.EVENTS_STREAM,
        description="Durable per-user events (alerts, zone changes); one sequence chain per user.",
        subjects=[subjects.EVENTS_INPUT],
        republish=RePublish(src=subjects.EVENTS_INPUT, dest=subjects.LIVE_EVENTS_PATTERN),
        retention=RetentionPolicy.LIMITS,
        discard=DiscardPolicy.OLD,
        storage=StorageType.FILE,
        max_age=topo.events_max_age_s,
        duplicate_window=topo.events_dedup_s,
        allow_direct=True,
        num_replicas=1,
    )


def partition_consumer(topo: Topology, partition: int) -> ConsumerConfig:
    name = subjects.engine_consumer(partition)
    return ConsumerConfig(
        name=name,
        durable_name=name,
        description=f"Geofence engine, telemetry partition {partition}",
        filter_subject=subjects.telemetry_partition(partition),
        deliver_policy=DeliverPolicy.ALL,
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=topo.consumer_ack_wait_s,
        max_ack_pending=topo.consumer_max_ack_pending,
        max_waiting=64,
    )


def buckets(topo: Topology) -> list[KeyValueConfig]:
    return [
        KeyValueConfig(
            bucket=subjects.KV_ENGINE,
            description="Engine membership heartbeats and partition leases",
            history=1,
            ttl=topo.lease_ttl_s,
            storage=StorageType.FILE,
        ),
        KeyValueConfig(
            bucket=subjects.KV_SESSIONS,
            description="Live sessions of every user across api replicas",
            history=1,
            ttl=topo.sessions_ttl_s,
            storage=StorageType.MEMORY,
        ),
        KeyValueConfig(
            bucket=subjects.KV_REVOKED,
            description="Revoked session tokens (remote sign-out)",
            history=1,
            ttl=topo.revoked_ttl_s,
            storage=StorageType.FILE,
        ),
    ]


async def ensure(js: JetStreamContext, topo: Topology) -> None:
    """Create or update everything; refuses to repartition an existing telemetry stream."""
    await _check_partitions(js, topo, missing_ok=True)
    for config in (telemetry_stream(topo), events_stream(topo)):
        await _ensure_stream(js, config)
    for partition in range(topo.partitions):
        await _ensure_consumer(js, partition_consumer(topo, partition))
    for bucket in buckets(topo):
        await _ensure_bucket(js, bucket)
    log.info("topology.ready", partitions=topo.partitions)


async def verify(js: JetStreamContext, topo: Topology) -> None:
    """Fail fast if streams, consumers or buckets are missing or partitioned differently."""
    await _check_partitions(js, topo, missing_ok=False)
    try:
        await js.stream_info(subjects.EVENTS_STREAM)
        for partition in range(topo.partitions):
            await js.consumer_info(subjects.TELEMETRY_STREAM, subjects.engine_consumer(partition))
        for bucket in buckets(topo):
            await js.key_value(bucket.bucket)
    except NotFoundError as exc:
        msg = f"broker topology is incomplete ({exc}); run the init job first"
        raise TopologyError(msg) from exc


async def _check_partitions(js: JetStreamContext, topo: Topology, *, missing_ok: bool) -> None:
    try:
        info = await js.stream_info(subjects.TELEMETRY_STREAM)
    except NotFoundError as exc:
        if missing_ok:
            return
        msg = "the TELEMETRY stream does not exist; run the init job first"
        raise TopologyError(msg) from exc
    transform = info.config.subject_transform
    existing = subjects.partitions_from_transform(transform.dest) if transform else None
    if existing != topo.partitions:
        msg = (
            f"TELEMETRY is partitioned into {existing} partitions but this deployment is "
            f"configured for {topo.partitions}; changing it would reorder device reports, so "
            "migrate the stream explicitly instead"
        )
        raise TopologyError(msg)


async def _ensure_stream(js: JetStreamContext, config: StreamConfig) -> None:
    assert config.name is not None
    try:
        await js.stream_info(config.name)
    except NotFoundError:
        await js.add_stream(config)
        log.info("topology.stream_created", stream=config.name)
        return
    await js.update_stream(config)


async def _ensure_consumer(js: JetStreamContext, config: ConsumerConfig) -> None:
    assert config.durable_name is not None
    try:
        await js.consumer_info(subjects.TELEMETRY_STREAM, config.durable_name)
    except NotFoundError:
        await js.add_consumer(subjects.TELEMETRY_STREAM, config)
        log.info("topology.consumer_created", consumer=config.durable_name)


async def _ensure_bucket(js: JetStreamContext, config: KeyValueConfig) -> None:
    try:
        await js.key_value(config.bucket)
    except BucketNotFoundError:
        await js.create_key_value(config)
        log.info("topology.bucket_created", bucket=config.bucket)
