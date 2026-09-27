import { signOut as signOutRequest } from "@/api/endpoints";
import { describeError, isApiError } from "@/api/http";
import { notify } from "@/features/shell/notices";
import { useSession } from "@/state/session";

let inFlight: Promise<void> | null = null;

/**
 * Sign this browser out. The session ends here only once the server has revoked it (204) or no
 * longer accepts it anyway (401). After any other answer the session cookie is still valid —
 * dropping local state then would only look signed out until the next page load signs straight
 * back in — so the user stays signed in and is offered another try.
 */
export function signOutHere(): Promise<void> {
  inFlight ??= signOutOnce().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

async function signOutOnce(): Promise<void> {
  try {
    await signOutRequest();
  } catch (error) {
    if (!isApiError(error, 401)) {
      notify({
        tone: "error",
        title: "Couldn't sign out",
        body: `You're still signed in. ${describeError(error)}`,
        action: { label: "Try again", run: () => void signOutHere() },
      });
      return;
    }
  }
  useSession.getState().signedOut();
}
