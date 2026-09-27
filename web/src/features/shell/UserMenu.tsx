import { Check, Keyboard, LogOut, Monitor, MonitorSmartphone, Moon, Sun } from "lucide-react";
import { DropdownMenu } from "radix-ui";
import type { ReactNode } from "react";
import { signOutHere } from "@/features/auth/signOut";
import { useSession } from "@/state/session";
import { type ThemePreference, useUi } from "@/state/ui";
import { cx } from "@/ui/cx";
import { Kbd } from "@/ui/Kbd";

export function initials(username: string): string {
  const parts = username.split(/[._-]+/).filter(Boolean);
  const letters = parts.length >= 2 ? `${parts[0]?.[0]}${parts[1]?.[0]}` : username.slice(0, 2);
  return letters.toUpperCase();
}

const THEMES: { value: ThemePreference; label: string; icon: ReactNode }[] = [
  { value: "light", label: "Light", icon: <Sun className="size-4" /> },
  { value: "dark", label: "Dark", icon: <Moon className="size-4" /> },
  { value: "system", label: "System", icon: <Monitor className="size-4" /> },
];

const itemClass =
  "flex h-9 cursor-default select-none items-center gap-2.5 rounded-lg px-2.5 text-[13px] text-ink outline-none data-[highlighted]:bg-surface-3";

export function UserMenu({ side = "right" }: { side?: "right" | "bottom" }) {
  const user = useSession((s) => s.user);
  const theme = useUi((s) => s.theme);
  if (!user) return null;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        aria-label={`Account: ${user.username}`}
        className="flex size-9 items-center justify-center rounded-full bg-[linear-gradient(135deg,var(--accent),#a78bfa)] font-semibold text-[12px] text-white shadow-[0_0_0_2px_var(--surface-solid),0_0_0_3px_var(--line-strong)] outline-none transition-transform hover:scale-105 focus-visible:shadow-[0_0_0_2px_var(--surface-solid),0_0_0_4px_var(--accent)]"
      >
        {initials(user.username)}
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          side={side}
          align="end"
          sideOffset={12}
          className="glass z-50 w-60 animate-rise rounded-2xl p-1.5"
        >
          <div className="px-2.5 pt-2 pb-2.5">
            <div className="text-[11px] text-muted">Signed in as</div>
            <div className="truncate font-mono font-semibold text-[13px] text-ink">
              {user.username}
            </div>
          </div>
          <DropdownMenu.Separator className="mx-1 my-1 h-px bg-line" />
          <DropdownMenu.Label className="px-2.5 pt-1.5 pb-1 text-[11px] text-muted">
            Theme
          </DropdownMenu.Label>
          <DropdownMenu.RadioGroup
            value={theme}
            onValueChange={(value) => useUi.getState().setTheme(value as ThemePreference)}
          >
            {THEMES.map((option) => (
              <DropdownMenu.RadioItem key={option.value} value={option.value} className={itemClass}>
                <span className="text-muted">{option.icon}</span>
                <span className="flex-1">{option.label}</span>
                <DropdownMenu.ItemIndicator>
                  <Check className="size-4 text-accent" />
                </DropdownMenu.ItemIndicator>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
          <DropdownMenu.Separator className="mx-1 my-1 h-px bg-line" />
          <DropdownMenu.Item
            className={itemClass}
            onSelect={() => useUi.getState().openPanel("sessions")}
          >
            <MonitorSmartphone className="size-4 text-muted" />
            <span className="flex-1">Sessions</span>
            <Kbd>S</Kbd>
          </DropdownMenu.Item>
          <DropdownMenu.Item
            className={itemClass}
            onSelect={() => useUi.getState().setShortcutsOpen(true)}
          >
            <Keyboard className="size-4 text-muted" />
            <span className="flex-1">Keyboard shortcuts</span>
            <Kbd>?</Kbd>
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="mx-1 my-1 h-px bg-line" />
          <DropdownMenu.Item
            className={cx(itemClass, "text-critical data-[highlighted]:bg-critical/10")}
            onSelect={() => void signOutHere()}
          >
            <LogOut className="size-4" />
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
