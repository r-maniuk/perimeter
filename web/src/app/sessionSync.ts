/**
 * One browser, one session. Every tab sends the same HttpOnly session cookie, so signing in or out
 * in one tab changes who every other tab's requests act for — while their live sockets, opened
 * with the previous sign-in, keep streaming for it. A tab that signs in or out tells the others;
 * they, and any tab coming back into view (a tab frozen in the background misses messages), ask
 * the server who the cookie belongs to now and follow it: to that account, as when a hello speaks
 * for another one, or to the sign-in card.
 *
 * A BroadcastChannel reaches every tab of this origin in the browser profile, in every browser the
 * dashboard runs in (it relies on AbortSignal.any, which came years later), so there is no
 * `storage` event fallback.
 */
import { currentUser } from "@/api/endpoints";
import type { User } from "@/api/schemas";
import { notify } from "@/features/shell/notices";
import { useSession } from "@/state/session";

export type SessionChange = "signedIn" | "signedOut";

/** Shown on the sign-in card of a tab signed out from elsewhere: another tab, or another device. */
export const SIGNED_OUT = "You were signed out. Sign in again.";
/** Shown when a tab finds its session gone and cannot tell why (expired, or signed out). */
export const SESSION_ENDED = "Your session has ended. Sign in again.";

const CHANNEL = "perimeter.session";

let channel: BroadcastChannel | null | undefined;

/** This tab's end of the channel all the dashboard's tabs in this browser share. */
function tabs(): BroadcastChannel | null {
  if (channel === undefined) {
    channel = typeof BroadcastChannel === "function" ? new BroadcastChannel(CHANNEL) : null;
  }
  return channel;
}

/** Tell the other tabs of this browser that this one signed in or out. */
export function announceSession(change: SessionChange): void {
  tabs()?.postMessage({ change });
}

/**
 * Keep this tab following the browser's session: check again whenever another tab signs in or
 * out, and whenever this tab is shown. Returns what stops it.
 */
export function followOtherTabs(): () => void {
  const onMessage = (event: MessageEvent) => {
    const change = (event.data as { change?: unknown } | null)?.change;
    if (change === "signedOut") void followBrowserSession(SIGNED_OUT);
    else if (change === "signedIn") void followBrowserSession();
  };
  const onVisibility = () => {
    if (document.visibilityState === "visible") void followBrowserSession();
  };
  tabs()?.addEventListener("message", onMessage);
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    tabs()?.removeEventListener("message", onMessage);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}

let checks = 0;

/**
 * Ask the server who this browser is signed in as, and follow: sign in (a tab still starting, or
 * one another tab signed in for), switch to the account another tab signed in as, or sign out
 * with `ended` on the card when nobody is signed in any more.
 *
 * Only the newest check counts, and only while the session is as it was when the check was sent:
 * a sign-in or sign-out made meanwhile is newer than the cookie the check went out with.
 */
export async function followBrowserSession(ended: string = SESSION_ENDED): Promise<void> {
  checks += 1;
  const check = checks;
  const asked = useSession.getState();
  let user: User | null;
  try {
    user = await currentUser();
  } catch {
    // The server cannot be reached. A tab still starting says so; any other keeps what it has
    // (its connection state shows the outage) and asks again at the next occasion.
    if (check === checks && useSession.getState() === asked && asked.status === "checking") {
      useSession.getState().signedOut("Can't reach the server right now.");
    }
    return;
  }
  const session = useSession.getState();
  if (check !== checks || session !== asked) return;
  if (!user) {
    // A tab still starting just shows the sign-in card; one that was signed in says why.
    if (session.status === "signedIn") session.signedOut(ended);
    else if (session.status === "checking") session.signedOut();
    return;
  }
  if (session.user?.id === user.id) return;
  session.signedIn(user);
  if (asked.status === "checking") return;
  notify({
    tone: "info",
    title: `Signed in as ${user.username}`,
    body:
      asked.status === "signedIn"
        ? "This browser switched accounts in another tab."
        : "Another tab of this browser signed in.",
  });
}
