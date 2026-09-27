import { AnimatePresence } from "motion/react";
import { lazy, Suspense, useEffect, useState } from "react";
import { onUnauthorized } from "@/api/http";
import type { User } from "@/api/schemas";
import { SignIn } from "@/features/auth/SignIn";
import { MapStage } from "@/map/MapStage";
import { useSession } from "@/state/session";
import { useUi } from "@/state/ui";
import { queryClient } from "./queryClient";
import { startRuntime, stopRuntime } from "./runtime";
import { followBrowserSession, followOtherTabs, SESSION_ENDED } from "./sessionSync";

/**
 * The signed-in workspace is its own chunk: the sign-in screen paints without it, and it is
 * fetched in the background while the user types a name.
 */
const loadShell = () => import("@/features/shell/Shell");
const Shell = lazy(() => loadShell().then((module) => ({ default: module.Shell })));

function prefetchShell(): () => void {
  if (typeof window.requestIdleCallback === "function") {
    const handle = window.requestIdleCallback(() => void loadShell(), { timeout: 3_000 });
    return () => window.cancelIdleCallback(handle);
  }
  const timer = setTimeout(() => void loadShell(), 1_500);
  return () => clearTimeout(timer);
}

export function SessionGate() {
  const status = useSession((s) => s.status);
  const user = useSession((s) => s.user);

  useEffect(() => {
    // Who the session cookie belongs to decides where this tab starts, and it keeps following it
    // as other tabs of this browser sign in and out.
    void followBrowserSession();
    const stopFollowing = followOtherTabs();
    const off = onUnauthorized(() => {
      if (useSession.getState().status === "signedIn") {
        useSession.getState().signedOut(SESSION_ENDED);
      }
    });
    return () => {
      stopFollowing();
      off();
    };
  }, []);

  useEffect(() => (status === "signedOut" ? prefetchShell() : undefined), [status]);

  return (
    <>
      <MapStage signedIn={status === "signedIn"} />
      <AnimatePresence>{status === "signedOut" && <SignIn key="sign-in" />}</AnimatePresence>
      {status === "signedIn" && user && <Workspace key={user.id} user={user} />}
    </>
  );
}

/**
 * One account's workspace. Keyed by the account, it is replaced whole when another account signs
 * in in place (from another tab of this browser): nothing of the last one — its runtime, its
 * cached zones and alerts, open panels, half-typed fields — carries over. Its views mount only
 * once the last account's cache has been cleared, so they never read what it left behind, and
 * every one of them loads the new account's data at once.
 */
function Workspace({ user }: { user: User }) {
  const [started, setStarted] = useState(false);

  useEffect(() => {
    startRuntime(user, queryClient);
    setStarted(true);
    return () => {
      stopRuntime();
      queryClient.clear();
      useUi.getState().select(null);
      useUi.getState().openPanel(null);
      useUi.getState().setDrawing(false);
    };
  }, [user]);

  return started ? (
    <Suspense fallback={null}>
      <Shell />
    </Suspense>
  ) : null;
}
