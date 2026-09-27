"""Outbox relay: move committed user events from PostgreSQL into the EVENTS stream.

Two paths, one guarantee (every committed event reaches JetStream at least once, and JetStream's
de-duplication window turns that into exactly once for subscribers):

* **Fast path** — the process that committed the events publishes them right away and deletes the
  rows it managed to publish. This is what keeps alert latency in milliseconds.
* **Sweeper** — every few seconds any instance claims rows that are older than a grace period
  (reserving them for a while in a short transaction of its own, so instances never fight over
  rows and nothing is held open while the broker answers), publishes and deletes them. It covers
  crashes, broker hiccups and database hiccups between commit and delete.

A crash between publishing and deleting republishes the same ``Nats-Msg-Id``; the stream drops it.
Each row keeps the trace context of the transaction that wrote it, and both paths publish the event
with it, so a trace follows an alert from the report that caused it to the sockets it is delivered
to, whichever path relayed it (the sweeper runs outside any span of its own).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import structlog
from prometheus_client import Counter, Gauge
from sqlalchemy.ext.asyncio import AsyncEngine

from perimeter.bus.publish import Ack, StreamPublisher
from perimeter.storage import outbox
from perimeter.storage.outbox import OutboxRow

log = structlog.get_logger(__name__)

RELAYED = Counter("perimeter_relay_published_total", "Outbox events published", ["path"])
RELAY_FAILURES = Counter("perimeter_relay_failures_total", "Outbox publish failures", ["path"])
RELAY_BACKLOG = Gauge("perimeter_relay_backlog", "Outbox rows waiting to be relayed")
RELAY_OLDEST = Gauge("perimeter_relay_oldest_seconds", "Age of the oldest unrelayed event")


class OutboxRelay:
    def __init__(
        self,
        engine: AsyncEngine,
        stream: StreamPublisher,
        *,
        publish_timeout_s: float = 5.0,
        sweep_interval_s: float = 1.0,
        sweep_min_age_s: float = 2.0,
        sweep_batch: int = 500,
    ) -> None:
        self._engine = engine
        self._stream = stream
        self._publish_timeout_s = publish_timeout_s
        self._sweep_interval_s = sweep_interval_s
        self._sweep_min_age_s = sweep_min_age_s
        self._sweep_batch = sweep_batch

    async def _publish(self, rows: Sequence[OutboxRow], path: str) -> list[int]:
        """Publish rows concurrently; return the ids JetStream acknowledged."""
        futures: list[asyncio.Future[Ack]] = []
        try:
            for row in rows:
                headers = {**(row.trace_context or {}), "Nats-Msg-Id": row.msg_id}
                futures.append(await self._stream.publish(row.subject, row.payload, headers))
        except BaseException:  # could not even send: nothing may keep waiting for an answer
            for future in futures:
                future.cancel()
            raise
        results = await asyncio.gather(
            *(asyncio.wait_for(f, self._publish_timeout_s) for f in futures),
            return_exceptions=True,
        )
        published: list[int] = []
        for row, result in zip(rows, results, strict=True):
            if isinstance(result, BaseException):
                RELAY_FAILURES.labels(path).inc()
                log.warning("relay.publish_failed", msg_id=row.msg_id, error=repr(result))
            else:
                published.append(row.id)
        RELAYED.labels(path).inc(len(published))
        return published

    async def relay(self, rows: Sequence[OutboxRow]) -> int:
        """Fast path: publish rows that were just committed, then delete them.

        Never raises: the change that wrote the rows is committed, and whatever this cannot finish
        the sweeper does (the stream drops an event published twice within its window).
        """
        if not rows:
            return 0
        try:
            published = await self._publish(rows, "fast")
        except Exception:  # the sweeper will pick the rows up
            log.exception("relay.fast_path_failed", rows=len(rows))
            return 0
        if published:
            try:
                async with self._engine.begin() as conn:
                    await outbox.delete(conn, published)
            except Exception as exc:  # published all the same; the sweeper clears the rows
                log.warning("relay.delete_failed", rows=len(published), error=repr(exc))
        return len(published)

    async def sweep_once(self) -> int:
        async with self._engine.begin() as conn:
            rows = await outbox.claim_stale(
                conn,
                min_age_s=self._sweep_min_age_s,
                limit=self._sweep_batch,
                hold_s=2 * self._publish_timeout_s,
            )
        if not rows:
            return 0
        published = await self._publish(rows, "sweeper")
        if published:
            async with self._engine.begin() as conn:
                await outbox.delete(conn, published)
            log.info("relay.swept", published=len(published), claimed=len(rows))
        return len(published)

    async def refresh_backlog_metrics(self) -> None:
        async with self._engine.connect() as conn:
            count, oldest = await outbox.backlog(conn)
        RELAY_BACKLOG.set(count)
        RELAY_OLDEST.set(oldest)

    async def run_sweeper(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                while await self.sweep_once() == self._sweep_batch:
                    pass
                await self.refresh_backlog_metrics()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("relay.sweep_failed")
            try:
                await asyncio.wait_for(stop.wait(), self._sweep_interval_s)
            except TimeoutError:
                continue
