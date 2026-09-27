/** Alerts received live, the toast stack, and the unseen counter on the rail. */
import { create } from "zustand";
import type { Alert } from "@/api/endpoints";
import { addAlert, expire, hold, type Toast } from "@/features/alerts/toasts";

const LIVE_LIMIT = 500;

interface AlertsState {
  live: Alert[];
  unseen: number;
  toasts: Toast[];
  receive(alert: Alert, options: { replayed: boolean; watching: boolean }): void;
  markSeen(): void;
  expireToasts(now: number): void;
  holdToast(id: string, ms: number): void;
  setToastExpiry(id: string, at: number): void;
  dismissToast(id: string): void;
  reset(): void;
}

export const useAlerts = create<AlertsState>()((set) => ({
  live: [],
  unseen: 0,
  toasts: [],
  receive: (alert, { replayed, watching }) =>
    set((s) => {
      if (s.live.some((a) => a.id === alert.id)) return s;
      const live = [alert, ...s.live].slice(0, LIVE_LIMIT);
      return {
        live,
        unseen: watching ? s.unseen : s.unseen + 1,
        // No toasts for alerts the timeline is already showing, or for history being replayed.
        toasts: replayed || watching ? s.toasts : addAlert(s.toasts, alert, Date.now()),
      };
    }),
  // Opening the timeline shows everything the toasts were announcing.
  markSeen: () => set({ unseen: 0, toasts: [] }),
  expireToasts: (now) =>
    set((s) => {
      const toasts = expire(s.toasts, now);
      return toasts.length === s.toasts.length ? s : { toasts };
    }),
  holdToast: (id, ms) => set((s) => ({ toasts: hold(s.toasts, id, ms) })),
  setToastExpiry: (id, at) =>
    set((s) => ({ toasts: s.toasts.map((t) => (t.id === id ? { ...t, expiresAt: at } : t)) })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  reset: () => set({ live: [], unseen: 0, toasts: [] }),
}));
