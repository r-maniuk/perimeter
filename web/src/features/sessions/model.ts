/**
 * How the viewer's live sessions relate to this tab.
 *
 * A sign-in (one token, usually one browser) can hold several sockets — one per open tab — and the
 * server marks every socket of the viewer's own sign-in `current`. Signing a session out revokes
 * its token, which closes every socket opened with it: signing out "another tab" of this browser
 * would sign this tab out too. So sockets of this sign-in are listed with this tab and never
 * offered for remote sign-out; only other sign-ins are.
 */
import type { LiveSession } from "@/api/schemas";

export interface SessionRow {
  session: LiveSession;
  /** Whether this is the socket of this very tab; `null` until this tab's socket said hello. */
  thisTab: boolean | null;
}

export interface SessionGroups {
  /** This sign-in: this tab first, then its other tabs, newest first. */
  here: SessionRow[];
  /** Other sign-ins (other browsers and devices), newest first. */
  elsewhere: LiveSession[];
}

export function connectedAt(session: LiveSession): number | null {
  const at = Date.parse(session.connected_at);
  return Number.isNaN(at) ? null : at;
}

function newestFirst(a: LiveSession, b: LiveSession): number {
  return (connectedAt(b) ?? 0) - (connectedAt(a) ?? 0);
}

/** `thisTab` is `hello.session_id` of this tab's socket, `null` while it is not connected. */
export function groupSessions(sessions: LiveSession[], thisTab: string | null): SessionGroups {
  const here: SessionRow[] = [];
  const elsewhere: LiveSession[] = [];
  for (const session of [...sessions].sort(newestFirst)) {
    if (thisTab !== null && session.sid === thisTab) here.unshift({ session, thisTab: true });
    else if (session.current) here.push({ session, thisTab: thisTab === null ? null : false });
    else elsewhere.push(session);
  }
  return { here, elsewhere };
}
