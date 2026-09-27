// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSession } from "@/state/session";
import { SignIn, validateUsername } from "./SignIn";

describe("username rules", () => {
  it.each([
    ["", "Enter a username."],
    ["a", "Use at least 2 characters."],
    ["-ada", "Start with a letter or a digit."],
    ["ada lovelace", "Use letters, digits, dots, dashes or underscores."],
    ["x".repeat(33), "Use at most 32 characters."],
  ])("rejects %j", (value, message) => {
    expect(validateUsername(value)).toBe(message);
  });

  it.each(["ada", "ada.lovelace", "g_hopper-1", "42"])("accepts %j", (value) => {
    expect(validateUsername(value)).toBeNull();
  });
});

describe("sign-in form", () => {
  beforeEach(() => {
    localStorage.clear();
    useSession.setState({ status: "signedOut", user: null, notice: null });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("prefills the last username and shows why a name is refused", async () => {
    localStorage.setItem("perimeter.lastUsername", "grace");
    render(<SignIn />);
    const input = screen.getByLabelText("Username");
    expect(input).toHaveProperty("value", "grace");
    await userEvent.clear(input);
    await userEvent.type(input, "g");
    await userEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(screen.getByRole("alert").textContent).toBe("Use at least 2 characters.");
    expect(input.getAttribute("aria-invalid")).toBe("true");
  });

  it("signs in, normalising the name, and remembers it", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({
        token: "t",
        token_type: "bearer",
        expires_at: "2026-09-27T12:00:00Z",
        user: { id: "u-1", username: "ada.lovelace" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SignIn />);
    await userEvent.type(screen.getByLabelText("Username"), "  Ada.Lovelace ");
    await userEvent.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() => expect(useSession.getState().status).toBe("signedIn"));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/v1/session");
    expect(JSON.parse(String(init.body))).toEqual({ username: "ada.lovelace" });
    expect(localStorage.getItem("perimeter.lastUsername")).toBe("ada.lovelace");
  });

  it("shows the server's reason when sign-in is refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json(
          {
            type: "https://perimeter.dev/problems/rate_limited",
            title: "Too Many Requests",
            status: 429,
            code: "rate_limited",
            detail: "slow down",
          },
          {
            status: 429,
            headers: { "retry-after": "12", "content-type": "application/problem+json" },
          },
        ),
      ),
    );
    render(<SignIn />);
    await userEvent.type(screen.getByLabelText("Username"), "ada");
    await userEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Too many attempts — try again in 12 s.",
    );
    expect(useSession.getState().status).toBe("signedOut");
  });
});
