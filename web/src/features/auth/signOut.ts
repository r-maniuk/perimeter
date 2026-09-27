import { signOut as signOutRequest } from "@/api/endpoints";
import { useSession } from "@/state/session";

/** Sign this browser out: revoke the token on the server, then drop local state regardless. */
export async function signOutHere(): Promise<void> {
  try {
    await signOutRequest();
  } catch {
    // The cookie may already be invalid or the server unreachable; the local sign-out stands.
  }
  useSession.getState().signedOut();
}
