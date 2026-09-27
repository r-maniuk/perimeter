/** Transient notices for things the user did not ask to see but should know (conflicts, errors). */
import { create } from "zustand";

export type NoticeTone = "info" | "success" | "warning" | "error";

export interface Notice {
  id: string;
  tone: NoticeTone;
  title: string;
  body?: string;
  action?: { label: string; run: () => void };
  expiresAt: number;
}

interface NoticeState {
  notices: Notice[];
  push(notice: Omit<Notice, "id" | "expiresAt"> & { durationMs?: number }): string;
  dismiss(id: string): void;
  expire(now: number): void;
}

let counter = 0;

export const useNotices = create<NoticeState>()((set) => ({
  notices: [],
  push: ({ durationMs, ...notice }) => {
    counter += 1;
    const id = `n${counter}`;
    const expiresAt = Date.now() + (durationMs ?? (notice.action ? 9_000 : 5_000));
    set((s) => ({ notices: [...s.notices, { ...notice, id, expiresAt }].slice(-3) }));
    return id;
  },
  dismiss: (id) => set((s) => ({ notices: s.notices.filter((n) => n.id !== id) })),
  expire: (now) =>
    set((s) => {
      const notices = s.notices.filter((n) => n.expiresAt > now);
      return notices.length === s.notices.length ? s : { notices };
    }),
}));

export function notify(notice: Omit<Notice, "id" | "expiresAt"> & { durationMs?: number }): string {
  return useNotices.getState().push(notice);
}
