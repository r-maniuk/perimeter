"""Identity: signed session tokens, revocation, device ingest credentials.

Authentication is intentionally simple (the brief allows a mocked login): a username is exchanged
for a signed, expiring token. Everything downstream authorises against the token's claims, so
swapping this module for a real identity provider leaves the rest of the service untouched.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass

import jwt
import structlog
from fastapi import Request, WebSocket
from nats.js import JetStreamContext
from nats.js.errors import KeyValueError
from nats.js.kv import KeyValue

from perimeter.config import SecuritySettings
from perimeter.wire import subjects

log = structlog.get_logger(__name__)

REVOCATIONS_SYNC_S = 10.0
REWATCH_EVERY_S = 60.0
REWATCH_DELAY_S = 0.5
LISTENER_QUEUE_MAX = 1_024

COOKIE_NAME = "perimeter_session"
ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32


class AuthError(Exception):
    """The request carries no usable identity."""


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    username: str
    token_id: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    principal: Principal


class TokenService:
    def __init__(self, settings: SecuritySettings) -> None:
        secret = settings.session_secret.get_secret_value()
        if len(secret.encode()) < MIN_SECRET_BYTES:
            msg = f"SESSION_SECRET must be at least {MIN_SECRET_BYTES} bytes"
            raise ValueError(msg)
        self._secret = secret
        self._ttl_s = settings.session_ttl_s

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    def issue(self, user_id: uuid.UUID, username: str) -> IssuedToken:
        now = int(time.time())
        principal = Principal(
            user_id=user_id,
            username=username,
            token_id=secrets.token_urlsafe(16),
            expires_at=now + self._ttl_s,
        )
        claims = {
            "sub": str(user_id),
            "name": username,
            "jti": principal.token_id,
            "iat": now,
            "exp": principal.expires_at,
        }
        return IssuedToken(jwt.encode(claims, self._secret, algorithm=ALGORITHM), principal)

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                options={"require": ["sub", "name", "jti", "exp", "iat"]},
            )
            return Principal(
                user_id=uuid.UUID(claims["sub"]),
                username=str(claims["name"]),
                token_id=str(claims["jti"]),
                expires_at=int(claims["exp"]),
            )
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise AuthError("invalid or expired session token") from exc


class RevocationsUnavailable(Exception):  # noqa: N818 - a condition the caller reports
    """The revoked tokens could not be read: a replica must not serve without them."""


class RevocationList:
    """Token ids revoked by remote sign-out, mirrored from the ``revoked`` bucket into memory.

    Every replica watches the bucket, so a sign-out on one replica is enforced by all of them
    within milliseconds, without a round trip per request. The mirror fails closed: a replica does
    not start before it has read every revocation, and a watch that ends is started again. Every
    minute the whole bucket is read anew, because a watch can also stall without a word (a broker
    whose store was lost resumes it at a sequence that no longer exists). A revocation is forgotten
    ``keep_s`` after it was seen, when every token it can concern has expired (the bucket keeps
    entries for the same time).
    """

    def __init__(self, kv: KeyValue, *, keep_s: float) -> None:
        self._kv = kv
        self._keep_s = keep_s
        self._revoked: dict[str, float] = {}  # token id -> when this replica learnt of it
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[asyncio.Queue[str]] = []

    @classmethod
    async def open(cls, js: JetStreamContext, *, keep_s: float) -> RevocationList:
        revocations = cls(await js.key_value(subjects.KV_REVOKED), keep_s=keep_s)
        await revocations.start()
        return revocations

    async def start(self, *, sync_timeout_s: float = REVOCATIONS_SYNC_S) -> None:
        synced = asyncio.Event()
        self._task = asyncio.create_task(self._follow(synced), name="revocations")
        try:
            await asyncio.wait_for(synced.wait(), sync_timeout_s)
        except TimeoutError as exc:
            await self.close()
            msg = f"the revoked tokens could not be read within {sync_timeout_s:g} s"
            raise RevocationsUnavailable(msg) from exc

    async def _follow(self, synced: asyncio.Event) -> None:
        delay = REWATCH_DELAY_S
        while True:
            try:
                watcher = await self._kv.watchall()
                try:
                    async with asyncio.timeout(REWATCH_EVERY_S):
                        async for entry in watcher:
                            if entry is None:  # every current value delivered
                                synced.set()
                                delay = REWATCH_DELAY_S
                            elif entry.operation is None and entry.key:
                                self._add(entry.key)
                except TimeoutError:
                    pass  # read everything anew, in case the watch stalled unnoticed
                finally:
                    with suppress(Exception):
                        await watcher.stop()  # type: ignore[no-untyped-call]
                self._prune()
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("revocations.watch_lost", error=repr(exc), retry_in_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, REWATCH_EVERY_S)

    def _add(self, token_id: str) -> None:
        if token_id in self._revoked:
            return
        self._revoked[token_id] = time.time()
        for queue in self._listeners:
            if queue.full():  # nobody is draining it: its reader sweeps sessions anyway
                queue.get_nowait()
            queue.put_nowait(token_id)

    def _prune(self) -> None:
        horizon = time.time() - self._keep_s
        for token_id in [t for t, seen in self._revoked.items() if seen < horizon]:
            del self._revoked[token_id]

    async def revoke(self, token_id: str) -> None:
        self._add(token_id)
        await self._kv.put(token_id, b"1")

    def is_revoked(self, token_id: str) -> bool:
        return token_id in self._revoked

    def __len__(self) -> int:
        return len(self._revoked)

    def subscribe(self) -> asyncio.Queue[str]:
        """Bounded queue of token ids revoked from now on (used to close live sessions)."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=LISTENER_QUEUE_MAX)
        self._listeners.append(queue)
        return queue

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError, KeyValueError):
                await self._task


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None


def request_token(request: Request) -> str | None:
    return _bearer(request.headers.get("authorization")) or request.cookies.get(COOKIE_NAME)


@dataclass(frozen=True, slots=True)
class SocketCredential:
    token: str
    ambient: bool
    """True for a cookie: the browser attaches it whichever page opened the socket."""


def websocket_credential(websocket: WebSocket) -> SocketCredential | None:
    """The token a socket authenticates with.

    A bearer header wins: no other site can make a browser send it, so it needs no origin check.
    The session cookie is ambient authority — it rides along with any socket a page opens to us —
    so it only counts together with an allowed ``Origin`` (:func:`origin_allowed`), which is what
    stops cross-site WebSocket hijacking. Tokens in the URL are not accepted: URLs end up in
    proxy logs, traces and browser history.
    """
    explicit = _bearer(websocket.headers.get("authorization"))
    if explicit:
        return SocketCredential(explicit, ambient=False)
    cookie = websocket.cookies.get(COOKIE_NAME)
    return SocketCredential(cookie, ambient=True) if cookie else None


def origin_allowed(websocket: WebSocket, allowed: frozenset[str]) -> bool:
    """Whether the handshake comes from one of our own pages (browsers always send ``Origin``)."""
    origin = websocket.headers.get("origin")
    return origin is not None and origin.rstrip("/") in allowed


def ingest_token_valid(presented: str | None, expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def device_credential(request_or_socket: Request | WebSocket) -> str | None:
    headers = request_or_socket.headers
    return _bearer(headers.get("authorization")) or headers.get("x-ingest-token")
