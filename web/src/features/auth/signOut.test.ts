import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError } from "@/api/http";
import { useNotices } from "@/features/shell/notices";
import { useSession } from "@/state/session";
import { signOutHere } from "./signOut";

const endpoints = vi.hoisted(() => ({ signOut: vi.fn() }));

vi.mock("@/api/endpoints", () => ({ signOut: endpoints.signOut }));

const ADA = { id: "u-ada", username: "ada" };

beforeEach(() => {
  useSession.getState().signedIn(ADA);
});

afterEach(() => {
  endpoints.signOut.mockReset();
  useNotices.setState({ notices: [] });
});

const unavailable = () =>
  new ApiError(
    503,
    { status: 503, code: "revocation_unavailable", detail: "the sign-out could not be recorded" },
    1,
  );

describe("signing out", () => {
  it("ends the session once the server has revoked it", async () => {
    endpoints.signOut.mockResolvedValue(undefined);
    await signOutHere();
    expect(useSession.getState()).toMatchObject({ status: "signedOut", user: null });
    expect(useNotices.getState().notices).toEqual([]);
  });

  it("ends the session when the server no longer accepts it anyway", async () => {
    endpoints.signOut.mockRejectedValue(new ApiError(401, { code: "unauthorized" }, null));
    await signOutHere();
    expect(useSession.getState().status).toBe("signedOut");
  });

  it.each([
    ["the revocation cannot be recorded", unavailable()],
    ["the server cannot be reached", new NetworkError("network unavailable")],
  ])("keeps the session and offers a retry when %s", async (_, error) => {
    endpoints.signOut.mockRejectedValueOnce(error);
    await signOutHere();
    expect(useSession.getState()).toMatchObject({ status: "signedIn", user: ADA });
    const [notice] = useNotices.getState().notices;
    expect(notice).toMatchObject({ tone: "error", title: "Couldn't sign out" });
    expect(notice?.body).toMatch(/^You're still signed in\./);

    endpoints.signOut.mockResolvedValueOnce(undefined);
    notice?.action?.run();
    await vi.waitFor(() => expect(useSession.getState().status).toBe("signedOut"));
  });

  it("sends one request for repeated clicks", async () => {
    let answer!: () => void;
    endpoints.signOut.mockReturnValue(
      new Promise<void>((resolve) => {
        answer = resolve;
      }),
    );
    const first = signOutHere();
    const second = signOutHere();
    answer();
    await Promise.all([first, second]);
    expect(endpoints.signOut).toHaveBeenCalledTimes(1);
    expect(useSession.getState().status).toBe("signedOut");
  });
});
