import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, describeError, requestEmpty } from "./http";

afterEach(() => vi.unstubAllGlobals());

function problem(status: number, body: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json(body, { status, headers: { "content-type": "application/problem+json" } }),
    ),
  );
}

describe("problem details", () => {
  it.each([
    "/problems/revocation_unavailable",
    "https://perimeter.dev/problems/revocation_unavailable",
    undefined,
  ])("reads the code whatever the type URI looks like (%s)", async (type) => {
    problem(503, {
      ...(type === undefined ? {} : { type }),
      title: "Service Unavailable",
      status: 503,
      detail: "the sign-out could not be recorded; retry",
      code: "revocation_unavailable",
    });
    const error = await requestEmpty("/v1/session", { method: "DELETE" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 503, code: "revocation_unavailable" });
    expect(describeError(error)).toBe("The server had a problem. Please try again.");
  });
});
