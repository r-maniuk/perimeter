"""Every NATS subject, stream, consumer and bucket name in one place.

Subject map::

    tlm.<device>                 device reports (stored by TELEMETRY as tlm.<partition>.<device>)
    evt.<user>                   durable user events (EVENTS stream, one sequence chain per user)
    live.evt.<user>              EVENTS republished with Nats-Sequence / Nats-Last-Sequence headers
    live.occ.<user>              ephemeral "reported from inside" pulses for a user's zones
    live.ses.<user>              a session of this user connected or disconnected
    pos.<d1>.<d2>...<dZ>         binary position frames of the leaf tile with quadkey d1..dZ
    ctl.ses.<session>            control messages for one live session (remote sign-out)
    sys.metrics.<service>.<id>   one-second metric heartbeats for the ops view
    _INBOX.<user>.>              replies to one authenticated service (per-user inbox prefix)
"""

from __future__ import annotations

TELEMETRY_STREAM = "TELEMETRY"
EVENTS_STREAM = "EVENTS"

KV_ENGINE = "engine"
KV_SESSIONS = "sessions"
KV_REVOKED = "revoked"

TELEMETRY_INPUT = "tlm.*"
EVENTS_INPUT = "evt.*"
LIVE_EVENTS_PATTERN = "live.evt.{{wildcard(1)}}"
METRICS_ALL = "sys.metrics.>"
POSITIONS_ALL = "pos.>"


def telemetry(device_id: str) -> str:
    return f"tlm.{device_id}"


def telemetry_partition_transform(partitions: int) -> str:
    return f"tlm.{{{{partition({partitions},1)}}}}.{{{{wildcard(1)}}}}"


def telemetry_partition(partition: int) -> str:
    return f"tlm.{partition}.*"


def telemetry_of_device(device_id: str) -> str:
    return f"tlm.*.{device_id}"


def partition_of(stored_subject: str) -> int:
    """Partition number of a stored telemetry subject ``tlm.<p>.<device>``."""
    _, partition, _ = stored_subject.split(".", 2)
    return int(partition)


def device_of(stored_subject: str) -> str:
    return stored_subject.rsplit(".", 1)[-1]


def engine_consumer(partition: int) -> str:
    return f"engine-p{partition}"


def events(user_id: object) -> str:
    return f"evt.{user_id}"


def live_events(user_id: object) -> str:
    return f"live.evt.{user_id}"


def live_pulses(user_id: object) -> str:
    return f"live.occ.{user_id}"


def live_sessions(user_id: object) -> str:
    return f"live.ses.{user_id}"


def live_of_user(user_id: object) -> str:
    """One subscription for everything live of one user: events, pulses and session notices."""
    return f"live.*.{user_id}"


def session_control(session_id: str) -> str:
    return f"ctl.ses.{session_id}"


def metrics_heartbeat(service: str, instance: str) -> str:
    return f"sys.metrics.{service}.{instance}"


def inbox_prefix(user: str) -> str:
    """Inbox root of an authenticated service; the broker lets each user read only its own."""
    return f"_INBOX.{user}"


def partitions_from_transform(destination: str) -> int | None:
    """Read ``P`` back from a ``tlm.{{partition(P,1)}}...`` transform destination."""
    marker = "partition("
    start = destination.find(marker)
    if start < 0:
        return None
    end = destination.find(",", start)
    try:
        return int(destination[start + len(marker) : end])
    except ValueError:
        return None
