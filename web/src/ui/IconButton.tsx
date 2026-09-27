import { type ButtonHTMLAttributes, forwardRef, type ReactNode } from "react";
import { cx } from "./cx";
import { Hint } from "./Hint";

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  shortcut?: string;
  active?: boolean;
  size?: "sm" | "md" | "lg";
  side?: "top" | "right" | "bottom" | "left";
  children: ReactNode;
}

const sizes = { sm: "size-7 rounded-lg", md: "size-9 rounded-[10px]", lg: "size-10 rounded-xl" };

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  {
    label,
    shortcut,
    active,
    size = "md",
    side = "right",
    className,
    children,
    type = "button",
    ...props
  },
  ref,
) {
  return (
    <Hint label={label} shortcut={shortcut} side={side}>
      <button
        ref={ref}
        type={type}
        aria-label={label}
        aria-pressed={active}
        className={cx(
          "relative inline-flex shrink-0 items-center justify-center text-ink-2 transition-[background-color,color] duration-150 hover:bg-surface-3 hover:text-ink disabled:opacity-40",
          active && "bg-accent-soft text-accent hover:bg-accent-soft hover:text-accent",
          sizes[size],
          className,
        )}
        {...props}
      >
        {children}
      </button>
    </Hint>
  );
});
