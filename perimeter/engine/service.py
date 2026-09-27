"""The geofence engine: one instance of ``python -m perimeter.engine``.

Wires the pieces together and owns their lifecycle:

* the partition :class:`~perimeter.engine.coordinator.Coordinator`, which starts and stops one
  :class:`~perimeter.engine.worker.PartitionWorker` per owned partition, fed by a KV watch so it
  reacts to peers joining, leaving or handing partitions over without waiting for its next round;
* the :class:`~perimeter.engine.tiles.TilePublisher` every worker hands committed movement to;
* the outbox sweeper, which relays events that a crash or a broker hiccup left behind;
* the event-loop lag monitor and the one-second heartbeat for the ops view.

Shutdown runs in dependency order: workers finish the batch in hand and ack it, leases and the
membership are given back (peers take the partitions over at once), the last tiles are flushed,
then the broker connection is drained and the database pool closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import asdict
from typing import Any

import sqlalchemy.exc
import structlog
from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext
from nats.js.kv import KeyValue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus import topology
from perimeter.bus.connection import connect
from perimeter.bus.leases import LeaseBucket
from perimeter.bus.publish import StreamPublisher
from perimeter.bus.relay import RELAY_BACKLOG, OutboxRelay
from perimeter.config import Settings
from perimeter.engine.batch import BatchProcessor, is_transient
from perimeter.engine.coordinator import Coordinator
from perimeter.engine.metrics import (
    TRACK_DEFAULT_ROWS,
    TRACK_FAILURES,
    TRACK_SLOTS,
    EngineStats,
    gauge_value,
    heartbeat_payload,
)
from perimeter.engine.tiles import TilePublisher
from perimeter.engine.worker import Backoff, LeaseHandle, PartitionWorker, unacknowledged
from perimeter.ops import tracing
from perimeter.ops.heartbeat import Heartbeat, instance_id
from perimeter.ops.looplag import LoopLagMonitor
from perimeter.storage import tracks
from perimeter.storage.engine import create_engine
from perimeter.wire import subjects

log = structlog.get_logger(__name__)

POOL_HEADROOM = 2
SHUTDOWN_GRACE_S = 10.0
TASK_STOP_TIMEOUT_S = 5.0
DATABASE_ATTEMPTS = 30
# Track maintenance works in bounded steps: this long per round at most, and a round that left
# work over (a backlog after an outage) is followed by the next after a short breather.
TRACK_ROUND_S = 10.0
TRACK_CATCH_UP_PAUSE_S = 1.0


class StartupError(RuntimeError):
    """The engine cannot run in this environment (for example, the schema is missing)."""


def pool_size(settings: Settings) -> int:
    """Database connections one engine process can need at the same moment.

    A partition worker holds a connection only while its batch transaction runs (the outbox fast
    path deletes after the commit on the same worker, so never alongside it). The outbox sweeper
    holds one while it sweeps, and one more is headroom so that a connection being replaced
    (failed pre-ping, recycling) never makes a worker wait. Any instance may end up owning every
    partition when its peers are down, hence ``partitions + 2``: 18 with the default 16. A smaller
    pool would turn a failover into pool timeouts instead of into throughput.
    """
    return max(settings.database.pool_size, settings.telemetry.partitions + POOL_HEADROOM)


def lease_timeout(settings: Settings, ttl_s: float) -> float:
    """Timeout of each lease operation: short enough that a round never outlasts its interval."""
    return max(0.25, min(settings.nats.request_timeout_s, ttl_s / 6))


class EngineService:
    """One engine instance; :meth:`start` and :meth:`stop` bracket its life."""

    def __init__(self, settings: Settings, *, instance: str | None = None) -> None:
        self.settings = settings
        self.instance = instance_id(instance or settings.engine.instance_id)
        self._stats = EngineStats()
        self._looplag = LoopLagMonitor()
        self._stop_rounds = asyncio.Event()
        self._stop_tiles = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._db: AsyncEngine | None = None
        self._nc: NatsClient | None = None
        self._stream: StreamPublisher | None = None
        self._coordinator: Coordinator | None = None
        self._started = False
        self._stopping = False

    @property
    def partitions(self) -> list[int]:
        return self._coordinator.owned if self._coordinator is not None else []

    @property
    def coordinator(self) -> Coordinator:
        if self._coordinator is None:
            msg = "the engine has not been started"
            raise RuntimeError(msg)
        return self._coordinator

    async def start(self) -> None:
        """Connect, verify the environment, then start consuming. Raises if it cannot run."""
        settings = self.settings
        topo = topology.Topology.from_settings(settings)
        self._db = create_engine(
            settings.database,
            application_name=f"perimeter-engine/{self.instance}",
            pool_size=pool_size(settings),
        )
        tracing.instrument_database(self._db)
        try:
            self._nc = await connect(settings.nats, name=f"perimeter-engine/{self.instance}")
            js = self._nc.jetstream()
            await topology.verify(js, topo)
            await self._wait_for_database(self._db)
            ttl_s, kv, leases = await self._lease_bucket(self._nc)
        except BaseException:
            await self._close(drain=False)
            raise
        self._stream = StreamPublisher(self._nc)
        relay = OutboxRelay(self._db, self._stream)
        tiles = TilePublisher(
            self._nc, zoom=settings.live.tile_zoom, flush_ms=settings.live.tile_flush_ms
        )
        processor = BatchProcessor(self._db, relay, tiles, owner=self.instance, stats=self._stats)

        def worker(partition: int, lease: LeaseHandle) -> PartitionWorker:
            return self._worker(js, processor, partition, lease)

        self._coordinator = Coordinator(
            instance=self.instance,
            partitions=topo.partitions,
            leases=leases,
            workers=worker,
            ttl_s=ttl_s,
        )
        heartbeat = Heartbeat(
            self._nc, service="engine", instance=self.instance, snapshot=self.snapshot
        )
        self._spawn("looplag", self._looplag.run(self._stop))
        self._spawn("heartbeat", heartbeat.run(self._stop))
        self._spawn("sweeper", relay.run_sweeper(self._stop))
        self._spawn("tiles", tiles.run(self._stop_tiles))
        self._spawn("rounds", self._coordinator.run(self._stop_rounds))
        self._spawn("changes", self._follow_changes(kv, self._coordinator))
        self._spawn("tracks", self._maintain_tracks())
        self._started = True
        log.info(
            "engine.started",
            instance=self.instance,
            partitions=topo.partitions,
            lease_ttl_s=ttl_s,
            db_pool_size=pool_size(settings),
        )

    async def stop(self) -> None:
        """Graceful shutdown: hand every partition over, flush, drain, close."""
        if not self._started or self._stopping:
            return
        self._stopping = True
        self._stop_rounds.set()
        await self._finish("rounds")
        await self._cancel("changes")
        await self.coordinator.shutdown(grace_s=SHUTDOWN_GRACE_S)
        self._stop_tiles.set()
        await self._finish("tiles")
        self._stop.set()
        for name in list(self._tasks):
            await self._finish(name)
        await self._close(drain=True)
        log.info("engine.stopped", instance=self.instance)

    async def abort(self) -> None:
        """Stop at once without handing anything over, as a crash would; leases expire.

        Also finishes a graceful stop that was interrupted part-way.
        """
        if not self._started:
            return
        self._stopping = True
        await self.coordinator.abort()
        for name in list(self._tasks):
            await self._cancel(name)
        await self._close(drain=False)
        log.warning("engine.aborted", instance=self.instance)

    def snapshot(self) -> dict[str, Any]:
        """Heartbeat payload; each call starts a new measurement window."""
        return heartbeat_payload(
            partitions=self.partitions,
            window=self._stats.window(),
            loop_lag_p99_ms=self._looplag.percentile(0.99) * 1000,
            relay_backlog=int(gauge_value(RELAY_BACKLOG)),
        )

    def _worker(
        self,
        js: JetStreamContext,
        processor: BatchProcessor,
        partition: int,
        lease: LeaseHandle,
    ) -> PartitionWorker:
        async def subscribe() -> JetStreamContext.PullSubscription:
            return await js.pull_subscribe_bind(
                durable=subjects.engine_consumer(partition), stream=subjects.TELEMETRY_STREAM
            )

        async def in_flight() -> list[tuple[str, bytes]]:
            return await unacknowledged(js, partition)

        return PartitionWorker(
            partition,
            subscribe=subscribe,
            in_flight=in_flight,
            processor=processor,
            lease=lease,
            batch_max=self.settings.engine.batch_max,
            fetch_wait_s=self.settings.engine.fetch_wait_s,
            linger_s=self.settings.engine.linger_ms / 1000,
        )

    async def _lease_bucket(self, nc: NatsClient) -> tuple[float, KeyValue, LeaseBucket]:
        """The ``engine`` bucket, with the TTL it really has rather than the one configured.

        Renewal timing derives from the TTL; trusting a configured value that is longer than the
        bucket's would let every lease expire between two renewals.
        """
        configured = self.settings.engine.lease_ttl_s
        probe = nc.jetstream(timeout=lease_timeout(self.settings, configured))
        info = await probe.stream_info(f"KV_{subjects.KV_ENGINE}")
        ttl_s = info.config.max_age or configured
        if abs(ttl_s - configured) > 1e-3:
            log.warning(
                "engine.lease_ttl_mismatch", bucket_ttl_s=ttl_s, configured_ttl_s=configured
            )
        js = nc.jetstream(timeout=lease_timeout(self.settings, ttl_s))
        kv = await js.key_value(subjects.KV_ENGINE)
        return ttl_s, kv, LeaseBucket(kv)

    async def _wait_for_database(self, db: AsyncEngine) -> None:
        """Wait for PostgreSQL to accept connections; refuse to start without the schema."""
        delay = 0.25
        for attempt in range(1, DATABASE_ATTEMPTS + 1):
            try:
                async with db.connect() as conn:
                    await conn.execute(text("SELECT 1 FROM partition_epochs LIMIT 1"))
            except sqlalchemy.exc.ProgrammingError as exc:
                msg = "the database schema is missing; run the init job first"
                raise StartupError(msg) from exc
            except Exception as exc:
                if not is_transient(exc) or attempt == DATABASE_ATTEMPTS:
                    raise
                log.info(
                    "engine.database_retry", attempt=attempt, error=repr(exc), retry_in_s=delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
            else:
                return

    async def _maintain_tracks(self) -> None:
        """Roll the track partitions forward; every engine tries, one does it (advisory lock)."""
        assert self._db is not None
        db = self._db
        retention_min = self.settings.tracks.retention_min
        interval_s = self.settings.tracks.maintenance_s
        while not self._stop.is_set():
            pause = interval_s
            try:
                done = await tracks.roll(
                    db, retention_min=retention_min, time_budget_s=TRACK_ROUND_S
                )
            except Exception as exc:  # retried next round; inserts fall back to the default slot
                TRACK_FAILURES.inc()
                log.warning("engine.tracks_maintenance_failed", error=repr(exc))
            else:
                TRACK_SLOTS.labels("attached").inc(done.created)
                TRACK_SLOTS.labels("dropped").inc(done.dropped)
                TRACK_DEFAULT_ROWS.labels("moved").inc(done.moved)
                TRACK_DEFAULT_ROWS.labels("purged").inc(done.purged)
                TRACK_FAILURES.inc(done.failed)
                if done.more:
                    pause = TRACK_CATCH_UP_PAUSE_S
                    log.warning("engine.tracks_catching_up", **asdict(done))
                elif done.failed:
                    log.warning("engine.tracks_maintenance_incomplete", **asdict(done))
                elif done.created or done.dropped or done.moved or done.purged:
                    log.info("engine.tracks_rolled", **asdict(done))
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), pause)

    async def _follow_changes(self, kv: KeyValue, coordinator: Coordinator) -> None:
        """Feed bucket changes to the coordinator; its periodic rounds remain the safety net."""
        backoff = Backoff(initial_s=0.5, cap_s=10.0)
        while True:
            watcher = None
            try:
                watcher = await kv.watchall(meta_only=True)
                async for entry in watcher:
                    if entry is not None:
                        backoff.reset()
                        coordinator.notice(entry.key, entry.operation)
            except Exception as exc:
                log.warning("engine.watch_failed", error=repr(exc))
            finally:
                if watcher is not None:
                    with suppress(Exception):
                        await watcher.stop()  # type: ignore[no-untyped-call]
            await asyncio.sleep(backoff.next())

    def _spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        self._tasks[name] = asyncio.create_task(coro, name=f"engine-{name}")

    async def _finish(self, name: str) -> None:
        """Wait for a task that has been told to stop; cancel it if it does not."""
        task = self._tasks.pop(name, None)
        if task is None:
            return
        done, _ = await asyncio.wait({task}, timeout=TASK_STOP_TIMEOUT_S)
        if not done:
            log.warning("engine.task_stop_timeout", task=name)
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
        elif not task.cancelled() and task.exception() is not None:
            log.error("engine.task_failed", task=name, exc_info=task.exception())

    async def _cancel(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)

    async def _close(self, *, drain: bool) -> None:
        if self._stream is not None:
            await self._stream.close()
        if self._nc is not None and not self._nc.is_closed:
            with suppress(Exception):
                await (self._nc.drain() if drain else self._nc.close())
        if self._db is not None:
            await self._db.dispose()
