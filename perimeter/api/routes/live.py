"""``WS /v1/live``: the client realtime channel (protocol in :mod:`perimeter.api.live.protocol`).

Authentication happens before the handshake is accepted: session cookie (browsers, which must
also present an allowed ``Origin``) or ``?token=`` / bearer header (command-line clients), a valid
signature and a token that was not signed out. Failures are reported *after* accepting, as close
code 4003: a browser cannot read the status of a rejected handshake, but it can read a close code.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from perimeter.api.live.protocol import CloseCode
from perimeter.api.security import AuthError, Principal, origin_allowed, websocket_credential
from perimeter.api.state import AppState, state_of

router = APIRouter(tags=["live"])


def _authenticate(websocket: WebSocket, state: AppState) -> Principal | str:
    """The principal, or why the socket is refused."""
    credential = websocket_credential(websocket)
    if credential is None:
        return "sign in to continue"
    # A browser always sends Origin: whatever the credential, a page elsewhere is refused. Without
    # an Origin only an explicit token counts (a command-line client, not a page).
    from_browser = websocket.headers.get("origin") is not None
    if (credential.ambient or from_browser) and not origin_allowed(
        websocket, state.settings.security.origins
    ):
        return "origin not allowed"
    try:
        principal = state.tokens.verify(credential.token)
    except AuthError as exc:
        return str(exc)
    if state.revoked.is_revoked(principal.token_id):
        return "this session was signed out"
    return principal


@router.websocket("/live")
async def live(websocket: WebSocket) -> None:
    state = state_of(websocket)
    outcome = _authenticate(websocket, state)
    await websocket.accept()
    if isinstance(outcome, str):
        await websocket.close(CloseCode.FORBIDDEN, outcome)
        return
    await state.hub.serve(websocket, outcome)
