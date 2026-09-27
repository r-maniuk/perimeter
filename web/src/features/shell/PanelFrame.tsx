import { X } from "lucide-react";
import type { ReactNode, Ref } from "react";
import { cx } from "@/ui/cx";
import { IconButton } from "@/ui/IconButton";

/** Header + scrollable body shared by every panel and inspector. */
export function PanelFrame({
  title,
  subtitle,
  actions,
  onClose,
  closeLabel = "Close panel",
  toolbar,
  children,
  bodyClassName,
  titleId,
  bodyRef,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  onClose?: (() => void) | undefined;
  closeLabel?: string;
  toolbar?: ReactNode;
  children: ReactNode;
  bodyClassName?: string;
  titleId?: string | undefined;
  bodyRef?: Ref<HTMLElement>;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-start gap-2 px-5 pt-4 pb-3">
        <div className="min-w-0 flex-1">
          <h2
            id={titleId}
            className="truncate font-semibold text-[15px] text-ink tracking-[-0.01em]"
          >
            {title}
          </h2>
          {subtitle && <div className="mt-0.5 truncate text-[12px] text-muted">{subtitle}</div>}
        </div>
        {actions}
        {onClose && (
          <IconButton label={closeLabel} shortcut="Esc" size="sm" side="bottom" onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        )}
      </header>
      {toolbar && <div className="px-4 pb-3">{toolbar}</div>}
      {/* Focusable so keyboard users can scroll panels that hold no controls (the pipeline). */}
      <section
        ref={bodyRef}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be reachable by keyboard.
        tabIndex={0}
        aria-labelledby={titleId}
        className={cx(
          "scroll-quiet min-h-0 flex-1 overflow-y-auto outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset",
          bodyClassName,
        )}
      >
        {children}
      </section>
    </div>
  );
}
