// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "@/api/schemas";
import { useNotices } from "@/features/shell/notices";
import { useSession } from "@/state/session";
import {
  announceSession,
  followBrowserSession,
  followOtherTabs,
  SESSION_ENDED,
  SIGNED_OUT,
} from "./sessionSync";

const endpoints = vi.hoisted(() => ({ currentUser: vi.fn() }));

vi.mock("@/api/endpoints", () => endpoints);

const GRACE: User = { id: "u-grace", username: "grace" };
const ADA: User = { id: "u-ada", username: "ada" };

/** Another tab of this browser, on the channel every tab shares. */
let otherTab: BroadcastChannel;
let heard: unknown[];
let stopFollowing: () => void;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  otherTab = new BroadcastChannel("perimeter.session");
  heard = [];
  otherTab.addEventListener("message", (event) => heard.push(event.data));
  stopFollowing = followOtherTabs();
});

afterEach(() => {
  stopFollowing();
  otherTab.close();
  endpoints.currentUser.mockReset();
  useSession.setState({ status: "checking", user: null, notice: null });
  useNotices.setState({ notices: [] });
});

describe("another tab of this browser", () => {
  it("signing in as someone else switches this tab to that account", async () => {
    useSession.getState().signedIn(GRACE);
    endpoints.currentUser.mockResolvedValue(ADA);
    otherTab.postMessage({ change: "signedIn" });
    await vi.waitFor(() => expect(useSession.getState().user).toEqual(ADA));
    expect(useNotices.getState().notices).toEqual([
      expect.objectContaining({
        title: "Signed in as ada",
        body: "This browser switched accounts in another tab.",
      }),
    ]);
  });

  it("signing in takes this tab from the sign-in card into that account", async () => {
    useSession.getState().signedOut(SIGNED_OUT);
    endpoints.currentUser.mockResolvedValue(GRACE);
    otherTab.postMessage({ change: "signedIn" });
    await vi.waitFor(() => expect(useSession.getState()).toMatchObject({ user: GRACE }));
    expect(useNotices.getState().notices).toEqual([
      expect.objectContaining({ body: "Another tab of this browser signed in." }),
    ]);
  });

  it("signing out takes this tab to the sign-in card", async () => {
    useSession.getState().signedIn(GRACE);
    endpoints.currentUser.mockResolvedValue(null);
    otherTab.postMessage({ change: "signedOut" });
    await vi.waitFor(() =>
      expect(useSession.getState()).toMatchObject({ status: "signedOut", notice: SIGNED_OUT }),
    );
  });

  it("signing in as the same account again changes nothing here", async () => {
    useSession.getState().signedIn(GRACE);
    const before = useSession.getState();
    endpoints.currentUser.mockResolvedValue(GRACE);
    otherTab.postMessage({ change: "signedIn" });
    await vi.waitFor(() => expect(endpoints.currentUser).toHaveBeenCalled());
    await Promise.resolve();
    expect(useSession.getState()).toBe(before);
    expect(useNotices.getState().notices).toEqual([]);
  });

  it("saying something this tab does not understand is ignored", async () => {
    useSession.getState().signedIn(GRACE);
    otherTab.postMessage({ change: "renamed" });
    otherTab.postMessage("signedOut");
    otherTab.postMessage(null);
    // A message this tab does understand, after them, shows they have all been delivered.
    endpoints.currentUser.mockResolvedValue(GRACE);
    otherTab.postMessage({ change: "signedIn" });
    await vi.waitFor(() => expect(endpoints.currentUser).toHaveBeenCalledTimes(1));
  });
});

describe("this tab", () => {
  it("tells the other tabs when it signs in or out, and does not hear itself", async () => {
    announceSession("signedIn");
    announceSession("signedOut");
    await vi.waitFor(() =>
      expect(heard).toEqual([{ change: "signedIn" }, { change: "signedOut" }]),
    );
    expect(endpoints.currentUser).not.toHaveBeenCalled();
  });

  it("checks who the browser is signed in as each time it is shown", async () => {
    useSession.getState().signedIn(GRACE);
    endpoints.currentUser.mockResolvedValue(ADA);
    setVisibility("hidden");
    expect(endpoints.currentUser).not.toHaveBeenCalled();
    // Frozen in the background, it may have missed what the other tabs said.
    setVisibility("visible");
    await vi.waitFor(() => expect(useSession.getState().user).toEqual(ADA));
  });

  it("starts where the session cookie says, saying so only when the server is out of reach", async () => {
    endpoints.currentUser.mockResolvedValueOnce(null);
    await followBrowserSession();
    expect(useSession.getState()).toMatchObject({ status: "signedOut", notice: null });

    useSession.setState({ status: "checking", user: null, notice: null });
    endpoints.currentUser.mockResolvedValueOnce(GRACE);
    await followBrowserSession();
    expect(useSession.getState()).toMatchObject({ status: "signedIn", user: GRACE });
    expect(useNotices.getState().notices).toEqual([]);

    useSession.setState({ status: "checking", user: null, notice: null });
    endpoints.currentUser.mockRejectedValueOnce(new TypeError("offline"));
    await followBrowserSession();
    expect(useSession.getState().notice).toBe("Can't reach the server right now.");
  });

  it("keeps what it has when the server cannot be reached", async () => {
    useSession.getState().signedIn(GRACE);
    endpoints.currentUser.mockRejectedValueOnce(new TypeError("offline"));
    await followBrowserSession();
    expect(useSession.getState()).toMatchObject({ status: "signedIn", user: GRACE });
  });

  it("says the session ended when it finds nobody signed in, unless told why", async () => {
    useSession.getState().signedIn(GRACE);
    endpoints.currentUser.mockResolvedValueOnce(null);
    await followBrowserSession();
    expect(useSession.getState().notice).toBe(SESSION_ENDED);
  });

  it("lets no answer overrule a newer check, or a sign-in made here meanwhile", async () => {
    useSession.getState().signedOut();
    const first = deferred<User | null>();
    const second = deferred<User | null>();
    endpoints.currentUser.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const older = followBrowserSession();
    const newer = followBrowserSession();
    second.resolve(GRACE);
    await newer;
    first.resolve(null);
    await older;
    expect(useSession.getState().user).toEqual(GRACE);

    // Asked with the cookie as it was before this tab signed in as ada.
    const stale = deferred<User | null>();
    endpoints.currentUser.mockReturnValueOnce(stale.promise);
    const check = followBrowserSession();
    useSession.getState().signedIn(ADA);
    stale.resolve(GRACE);
    await check;
    expect(useSession.getState().user).toEqual(ADA);
  });
});
