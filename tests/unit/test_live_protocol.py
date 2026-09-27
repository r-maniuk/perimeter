import json
from datetime import UTC, datetime

import pytest

from perimeter.api.live import metrics
from perimeter.api.live.protocol import (
    MAX_CLIENT_MESSAGE_CHARS,
    RESYNC_POSITIONS,
    Hello,
    OpsToggle,
    Ping,
    ProtocolError,
    Resume,
    ResumeMode,
    ResumeView,
    Sessions,
    SessionView,
    UserView,
    Viewport,
    decode_client,
    encode,
)


def test_client_messages_decode_into_their_types() -> None:
    assert decode_client('{"type":"viewport","bbox":[4.8,52.3,5.0,52.4],"zoom":13.5}') == Viewport(
        bbox=(4.8, 52.3, 5.0, 52.4), zoom=13.5
    )
    assert decode_client('{"type":"viewport","bbox":[170,-10,190,10]}') == Viewport(
        bbox=(170, -10, 190, 10)
    )
    assert decode_client('{"type":"resume","after":42}') == Resume(after=42)
    assert decode_client('{"type":"ping","t":1790000000123}') == Ping(t=1_790_000_000_123)
    assert decode_client('{"type":"ping"}') == Ping()
    assert decode_client('{"type":"ops","on":true}') == OpsToggle(on=True)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"type":"teleport"}',
        '{"bbox":[0,0,1,1]}',
        '{"type":"viewport","bbox":[0,0,1]}',
        '{"type":"viewport","bbox":[0,-91,1,1]}',
        '{"type":"viewport","bbox":[0,0,1,1],"zoom":99}',
        '{"type":"resume","after":-1}',
        '{"type":"resume","after":"7"}',
        '{"type":"ops"}',
        '{"type":"ping","t":' + "1" * MAX_CLIENT_MESSAGE_CHARS + "}",
    ],
)
def test_anything_else_is_a_protocol_error(text: str) -> None:
    with pytest.raises(ProtocolError):
        decode_client(text)


def test_server_frames_are_tagged_objects() -> None:
    hello = json.loads(
        encode(
            Hello(
                session_id="s1",
                user=UserView(id="u1", username="alice"),
                server_time=1,
                protocol=1,
                resume=ResumeView(mode=ResumeMode.REPLAY, after=41),
                tile_zoom=12,
                replica="api-a",
            )
        )
    )
    assert next(iter(hello)) == "type"
    assert hello["type"] == "hello"
    assert hello["resume"] == {"mode": "replay", "after": 41}
    assert json.loads(RESYNC_POSITIONS) == {"type": "resync", "scope": "positions"}
    sessions = json.loads(
        encode(
            Sessions(
                sessions=[
                    SessionView(
                        sid="s1",
                        label="Chrome · macOS",
                        agent=None,
                        ip=None,
                        replica="api-a",
                        connected_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
                        current=True,
                    )
                ]
            )
        )
    )
    assert sessions["sessions"][0]["connected_at"] == "2026-09-26T12:00:00Z"


def test_rates_are_per_second_between_reads() -> None:
    now = [100.0]
    counters = metrics.Counters()
    rates = metrics.Rates(counters, clock=lambda: now[0])
    counters.sent += 50
    counters.dropped += 3
    now[0] += 2.0
    assert rates.read() == {"live_out_rate": 25.0, "live_drops_rate": 1.5}
    now[0] += 1.0
    assert rates.read() == {"live_out_rate": 0.0, "live_drops_rate": 0.0}
