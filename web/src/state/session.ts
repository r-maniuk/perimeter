/** Who is signed in. */
import { create } from "zustand";
import type { User } from "@/api/schemas";

type SessionStatus = "checking" | "signedOut" | "signedIn";

interface SessionState {
  status: SessionStatus;
  user: User | null;
  /** Shown on the sign-in card after an involuntary sign-out. */
  notice: string | null;
  /** How many times this tab has been signed out: each time gets a sign-in card of its own. */
  signOuts: number;
  signedIn(user: User): void;
  signedOut(notice?: string | null): void;
}

export const useSession = create<SessionState>()((set) => ({
  status: "checking",
  user: null,
  notice: null,
  signOuts: 0,
  signedIn: (user) => set({ status: "signedIn", user, notice: null }),
  signedOut: (notice = null) =>
    set((s) => ({ status: "signedOut", user: null, notice, signOuts: s.signOuts + 1 })),
}));

const LAST_USER_KEY = "perimeter.lastUsername";

export function rememberUsername(username: string): void {
  try {
    localStorage.setItem(LAST_USER_KEY, username);
  } catch {
    // Convenience only.
  }
}

export function lastUsername(): string {
  try {
    return localStorage.getItem(LAST_USER_KEY) ?? "";
  } catch {
    return "";
  }
}
