"""The client realtime protocol spoken on ``/v1/live``.

Server to client, text frames are JSON objects tagged by ``type``: ``hello`` (always first),
``event``, ``pulse``, ``sessions``, ``resync``, ``ops`` and ``pong``. Positions travel as binary
bundles of tile frames (:mod:`perimeter.wire.frames`). Client to server: ``viewport``, ``resume``,
``ping`` and ``ops``.

Frames relayed from NATS (events, pulses, position tiles) are forwarded as bytes and never
re-encoded; the frames this replica originates are msgspec structs, so encoding and decoding stay
in C and cost the event loop next to nothing.
"""

from __future__ import annotations

import time
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Annotated

import msgspec

PROTOCOL_VERSION = 1
MAX_CLIENT_MESSAGE_CHARS = 4_096


class CloseCode(IntEnum):
    """WebSocket close codes of the live channel (4xxx are specific to this protocol)."""

    GOING_AWAY = 1001  # the replica is shutting down: reconnect and resume
    PROTOCOL_ERROR = 1008  # the client sent something that is not part of the protocol
    INTERNAL_ERROR = 1011  # including a socket that did not accept a message in time
    TRY_AGAIN_LATER = 1013  # a dependency (broker) is unavailable right now
    SIGNED_OUT = 4001
    SESSION_EXPIRED = 4002  # the token that opened the socket expired: sign in again
    FORBIDDEN = 4003
    EVENTS_OVERFLOW = 4008  # the client fell behind its events: reconnect with resume_after
    TOO_MANY_SESSIONS = 4009


class ResumeMode(StrEnum):
    FRESH = "fresh"  # no resume point given: live events from now on
    REPLAY = "replay"  # everything after the resume point is replayed, then live
    RESET = "reset"  # the resume point is unusable: reload state over REST, then live


class ProtocolError(ValueError):
    """A client message that is not part of the protocol."""


Longitude = Annotated[float, msgspec.Meta(ge=-540.0, le=540.0)]  # panned maps leave [-180, 180]
Latitude = Annotated[float, msgspec.Meta(ge=-90.0, le=90.0)]
MapZoom = Annotated[float, msgspec.Meta(ge=0.0, le=30.0)]
SequenceNumber = Annotated[int, msgspec.Meta(ge=0)]


class Viewport(msgspec.Struct, frozen=True, tag="viewport", tag_field="type"):
    """The map area the client shows: ``bbox`` is ``[west, south, east, north]`` in degrees."""

    bbox: tuple[Longitude, Latitude, Longitude, Latitude]
    zoom: MapZoom | None = None


class Resume(msgspec.Struct, frozen=True, tag="resume", tag_field="type"):
    """Replay events after sequence ``after`` (only as the first message of a connection)."""

    after: SequenceNumber


class Ping(msgspec.Struct, frozen=True, tag="ping", tag_field="type"):
    """Application-level ping; ``t`` is echoed back in the ``pong``."""

    t: int | float | None = None


class OpsToggle(msgspec.Struct, frozen=True, tag="ops", tag_field="type"):
    """Start or stop the once-a-second pipeline metrics frames."""

    on: bool


type ClientMessage = Viewport | Resume | Ping | OpsToggle

_client_decoder: msgspec.json.Decoder[Viewport | Resume | Ping | OpsToggle] = msgspec.json.Decoder(
    Viewport | Resume | Ping | OpsToggle
)


def decode_client(text: str) -> ClientMessage:
    if len(text) > MAX_CLIENT_MESSAGE_CHARS:
        msg = f"messages are limited to {MAX_CLIENT_MESSAGE_CHARS} characters"
        raise ProtocolError(msg)
    try:
        return _client_decoder.decode(text)
    except msgspec.DecodeError as exc:  # also covers ValidationError
        raise ProtocolError(str(exc)) from exc


class UserView(msgspec.Struct, frozen=True):
    id: str
    username: str


class ResumeView(msgspec.Struct, frozen=True):
    mode: ResumeMode
    after: int  # the event frames that follow on this connection all have seq > after


class Hello(msgspec.Struct, frozen=True, tag="hello", tag_field="type"):
    session_id: str
    user: UserView
    server_time: int  # epoch milliseconds
    protocol: int
    resume: ResumeView
    tile_zoom: int
    replica: str


class Pong(msgspec.Struct, frozen=True, tag="pong", tag_field="type"):
    t: int | float | None
    server_time: int


class Resync(msgspec.Struct, frozen=True, tag="resync", tag_field="type"):
    scope: str


class SessionView(msgspec.Struct, frozen=True):
    """One of the user's live sessions; the recipient's own is ``hello.session_id``."""

    sid: str
    label: str
    agent: str | None
    ip: str | None
    replica: str
    connected_at: datetime
    current: bool  # opened with the recipient's sign-in (the same browser, typically)


class Sessions(msgspec.Struct, frozen=True, tag="sessions", tag_field="type"):
    sessions: list[SessionView]


_encoder = msgspec.json.Encoder()


def encode(frame: msgspec.Struct) -> bytes:
    return _encoder.encode(frame)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


RESYNC_POSITIONS = encode(Resync(scope="positions"))
