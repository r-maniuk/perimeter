import { AnimatePresence } from "motion/react";
import { lazy, Suspense, useEffect } from "react";
import { currentUser } from "@/api/endpoints";
import { onUnauthorized } from "@/api/http";
import { SignIn } from "@/features/auth/SignIn";
import { MapStage } from "@/map/MapStage";
import { useSession } from "@/state/session";
import { useUi } from "@/state/ui";
import { queryClient } from "./queryClient";
import { startRuntime, stopRuntime } from "./runtime";

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
    const controller = new AbortController();
    currentUser(controller.signal)
      .then((me) => {
        if (me) useSession.getState().signedIn(me);
        else useSession.getState().signedOut();
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          useSession.getState().signedOut("Can't reach the server right now.");
        }
      });
    const off = onUnauthorized(() => {
      if (useSession.getState().status === "signedIn") {
        useSession.getState().signedOut("Your session has ended. Sign in again.");
      }
    });
    return () => {
      controller.abort();
      off();
    };
  }, []);

  useEffect(() => (status === "signedOut" ? prefetchShell() : undefined), [status]);

  useEffect(() => {
    if (status !== "signedIn" || !user) return;
    startRuntime(user, queryClient);
    return () => {
      stopRuntime();
      queryClient.clear();
      useUi.getState().select(null);
      useUi.getState().openPanel(null);
      useUi.getState().setDrawing(false);
    };
  }, [status, user]);

  return (
    <>
      <MapStage signedIn={status === "signedIn"} />
      <AnimatePresence>{status === "signedOut" && <SignIn key="sign-in" />}</AnimatePresence>
      {status === "signedIn" && (
        <Suspense fallback={null}>
          <Shell />
        </Suspense>
      )}
    </>
  );
}
