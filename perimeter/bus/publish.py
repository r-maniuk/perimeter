"""Publishing to JetStream without waiting per message, and without leaving anything behind.

A stream stores a message and answers on its reply subject with an acknowledgement. Publishing
many messages and then waiting for all their answers costs one round trip per batch instead of one
per message. nats-py offers that as ``JetStreamContext.publish_async``, but it takes a
pending-publish slot and registers the answer's future *before* it publishes, and gives neither
back when the publish itself raises (a closed connection, a full reconnect buffer, an invalid
subject): enough such failures and every later publish waits for a slot forever.

:class:`StreamPublisher` does the same job with public client calls. One wildcard subscription on
the connection's own inbox receives every answer, and the future of a publish is forgotten on every
path: answered, failed, timed out, cancelled or never sent. It sets no limit of its own; callers
bound how much they keep in flight.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Mapping
from contextlib import suppress

import msgspec
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

_NO_RESPONDERS = "503"


class PublishError(Exception):
    """The stream did not store the message: it refused it, or no stream listens on the subject."""


class Ack(msgspec.Struct, frozen=True):
    stream: str
    seq: int
    duplicate: bool = False


class _ApiError(msgspec.Struct, frozen=True):
    code: int = 0
    err_code: int = 0
    description: str = ""


class _Answer(msgspec.Struct, frozen=True):
    stream: str = ""
    seq: int = 0
    duplicate: bool = False
    error: _ApiError | None = None


_decode_answer = msgspec.json.Decoder(_Answer).decode


class StreamPublisher:
    """Publishes to JetStream subjects; each publish returns a future of its acknowledgement."""

    def __init__(self, nc: NatsClient) -> None:
        self._nc = nc
        self._inbox = ""
        self._subscription: Subscription | None = None
        self._subscribing = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Ack]] = {}
        self._tokens = itertools.count(1)

    @property
    def pending(self) -> int:
        """Publishes still waiting for their acknowledgement."""
        return len(self._pending)

    async def publish(
        self, subject: str, payload: bytes, headers: Mapping[str, str] | None = None
    ) -> asyncio.Future[Ack]:
        """Send one message and return the future of its acknowledgement.

        Raises what the client raises when the message cannot even be sent; nothing stays pending
        then. A caller that stops waiting cancels the future, which forgets it as well.
        """
        inbox = self._inbox or await self._subscribe()
        token = format(next(self._tokens), "x")
        future: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        self._pending[token] = future
        future.add_done_callback(lambda _: self._pending.pop(token, None))
        try:
            await self._nc.publish(
                subject, payload, reply=f"{inbox}.{token}", headers=dict(headers or {})
            )
        except BaseException:
            del self._pending[token]  # now, not when the cancellation's callbacks run
            future.cancel()
            raise
        return future

    async def close(self) -> None:
        """Stop listening for acknowledgements; publishes still waiting are cancelled."""
        subscription, self._subscription, self._inbox = self._subscription, None, ""
        if subscription is not None:
            with suppress(Exception):  # the connection may be gone already
                await subscription.unsubscribe()
        for future in list(self._pending.values()):
            future.cancel()

    async def _subscribe(self) -> str:
        async with self._subscribing:
            if not self._inbox:
                inbox = self._nc.new_inbox()
                self._subscription = await self._nc.subscribe(f"{inbox}.*", cb=self._on_answer)
                self._inbox = inbox
        return self._inbox

    async def _on_answer(self, msg: Msg) -> None:
        future = self._pending.get(msg.subject.rpartition(".")[2])
        if future is None or future.done():
            return  # its publisher stopped waiting
        if msg.headers and msg.headers.get("Status") == _NO_RESPONDERS:
            future.set_exception(PublishError(f"no stream listens on {msg.subject!r}"))
            return
        try:
            answer = _decode_answer(msg.data)
        except msgspec.DecodeError as exc:
            future.set_exception(PublishError(f"unreadable acknowledgement: {exc}"))
            return
        if answer.error is not None:
            error = answer.error
            future.set_exception(
                PublishError(f"{error.code} ({error.err_code}): {error.description}")
            )
        else:
            future.set_result(Ack(answer.stream, answer.seq, answer.duplicate))
