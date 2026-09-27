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


class RevocationList:
    """Token ids revoked by remote sign-out, mirrored from the ``revoked`` bucket into memory.

    Every replica watches the bucket, so a sign-out on one replica is enforced by all of them
    within milliseconds, without a round trip per request.
    """

    def __init__(self, kv: KeyValue) -> None:
        self._kv = kv
        self._revoked: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[asyncio.Queue[str]] = []

    @classmethod
    async def open(cls, js: JetStreamContext) -> RevocationList:
        revocations = cls(await js.key_value(subjects.KV_REVOKED))
        await revocations.start()
        return revocations

    async def start(self) -> None:
        watcher = await self._kv.watchall()
        ready = asyncio.Event()

        async def follow() -> None:
            async for entry in watcher:
                if entry is None:  # initial values delivered
                    ready.set()
                    continue
                if entry.operation is None and entry.key:
                    self._revoked.add(entry.key)
                    for queue in self._listeners:
                        queue.put_nowait(entry.key)

        self._task = asyncio.create_task(follow(), name="revocations")
        try:
            await asyncio.wait_for(ready.wait(), 10)
        except TimeoutError:
            log.warning("revocations.initial_sync_slow")

    async def revoke(self, token_id: str) -> None:
        self._revoked.add(token_id)
        await self._kv.put(token_id, b"1")

    def is_revoked(self, token_id: str) -> bool:
        return token_id in self._revoked

    def subscribe(self) -> asyncio.Queue[str]:
        """Queue of token ids revoked from now on (used to close live sessions)."""
        queue: asyncio.Queue[str] = asyncio.Queue()
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
