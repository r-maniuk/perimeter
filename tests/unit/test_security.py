import time
import uuid
from unittest.mock import MagicMock

import jwt
import pytest
from pydantic import SecretStr

from perimeter.api.security import (
    AuthError,
    SocketCredential,
    TokenService,
    ingest_token_valid,
    origin_allowed,
    websocket_credential,
)
from perimeter.config import SecuritySettings

SECRET = "0123456789abcdef0123456789abcdef-long-enough"


def service(ttl: int = 3_600) -> TokenService:
    return TokenService(SecuritySettings(session_secret=SecretStr(SECRET), session_ttl_s=ttl))


def test_issued_tokens_verify_back_to_the_same_principal() -> None:
    user = uuid.uuid4()
    issued = service().issue(user, "alice")
    principal = service().verify(issued.token)
    assert principal == issued.principal
    assert principal.user_id == user
    assert principal.expires_at > time.time()


def test_tampered_foreign_and_expired_tokens_are_rejected() -> None:
    issued = service().issue(uuid.uuid4(), "alice")
    with pytest.raises(AuthError):
        service().verify(issued.token[:-2] + "xx")
    foreign = jwt.encode({"sub": str(uuid.uuid4())}, "other-secret-" * 4, algorithm="HS256")
    with pytest.raises(AuthError):
        service().verify(foreign)
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "name": "a", "jti": "x", "iat": 1, "exp": 2},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        service().verify(expired)
    unsigned = jwt.encode({"sub": "x"}, key="", algorithm="none")
    with pytest.raises(AuthError):
        service().verify(unsigned)


def test_short_secrets_are_refused() -> None:
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        TokenService(SecuritySettings(session_secret=SecretStr("short")))


def test_ingest_token_comparison() -> None:
    assert ingest_token_valid("abc", "abc")
    assert not ingest_token_valid("abd", "abc")
    assert not ingest_token_valid(None, "abc")
    assert not ingest_token_valid("abc", "")


def _socket(origin: str | None, cookie: bool) -> MagicMock:
    socket = MagicMock()
    socket.headers = {"origin": origin} if origin else {}
    socket.cookies = {"perimeter_session": "t"} if cookie else {}
    return socket


def test_origin_check_protects_cookie_authenticated_sockets() -> None:
    allowed = frozenset({"http://localhost:8080"})
    assert origin_allowed(_socket("http://localhost:8080/", cookie=True), allowed)
    assert not origin_allowed(_socket("https://evil.test", cookie=True), allowed)
    assert not origin_allowed(_socket(None, cookie=True), allowed)


def test_explicit_tokens_win_over_the_ambient_cookie() -> None:
    socket = _socket(None, cookie=True)
    socket.query_params = {"token": "explicit"}
    assert websocket_credential(socket) == SocketCredential("explicit", ambient=False)
    cookie_only = _socket("http://localhost:8080", cookie=True)
    cookie_only.query_params = {}
    assert websocket_credential(cookie_only) == SocketCredential("t", ambient=True)
    nothing = _socket(None, cookie=False)
    nothing.query_params = {}
    assert websocket_credential(nothing) is None
