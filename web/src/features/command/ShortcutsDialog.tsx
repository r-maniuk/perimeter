import { X } from "lucide-react";
import { Dialog } from "radix-ui";
import { useUi } from "@/state/ui";
import { Kbd } from "@/ui/Kbd";

const GROUPS: { title: string; keys: [string[], string][] }[] = [
  {
    title: "Panels",
    keys: [
      [["Z"], "Zones"],
      [["A"], "Alerts"],
      [["F"], "Fleet"],
      [["S"], "Sessions"],
      [["O"], "Pipeline"],
    ],
  },
  {
    title: "Map",
    keys: [
      [["D"], "Draw a zone"],
      [["L"], "Follow the selected device"],
      [["Esc"], "Cancel · deselect · close"],
      [["Shift", "drag"], "Zoom to a box"],
      [["Ctrl", "drag"], "Rotate and tilt"],
    ],
  },
  {
    title: "General",
    keys: [
      [["⌘", "K"], "Search and commands"],
      [["T"], "Toggle theme"],
      [["?"], "This list"],
    ],
  },
];

export function ShortcutsDialog() {
  const open = useUi((s) => s.shortcutsOpen);
  return (
    <Dialog.Root open={open} onOpenChange={(next) => useUi.getState().setShortcutsOpen(next)}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/20 backdrop-blur-[2px]" />
        <Dialog.Content className="glass fixed top-1/2 left-1/2 z-50 w-[min(94vw,520px)] -translate-x-1/2 -translate-y-1/2 animate-rise rounded-3xl p-6">
          <div className="flex items-center justify-between">
            <Dialog.Title className="font-semibold text-[16px] text-ink">
              Keyboard shortcuts
            </Dialog.Title>
            <Dialog.Close
              aria-label="Close"
              className="flex size-8 items-center justify-center rounded-lg text-muted hover:bg-surface-3 hover:text-ink"
            >
              <X className="size-4" />
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">
            Keys that work anywhere on the map.
          </Dialog.Description>
          <div className="mt-4 grid gap-x-8 gap-y-5 sm:grid-cols-2">
            {GROUPS.map((group) => (
              <section key={group.title}>
                <h3 className="font-semibold text-[11px] text-muted uppercase tracking-[0.08em]">
                  {group.title}
                </h3>
                <ul className="mt-2 space-y-2">
                  {group.keys.map(([keys, label]) => (
                    <li
                      key={label}
                      className="flex items-center justify-between gap-3 text-[13px] text-ink"
                    >
                      {label}
                      <span className="flex gap-1">
                        {keys.map((k) => (
                          <Kbd key={k}>{k}</Kbd>
                        ))}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
