import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cx } from "./cx";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

const variants: Record<Variant, string> = {
  primary:
    "bg-accent text-accent-ink shadow-[0_1px_0_rgb(255_255_255/0.2)_inset,0_6px_16px_-8px_var(--accent)] hover:bg-accent-strong active:translate-y-px disabled:opacity-50",
  secondary:
    "bg-surface-2 text-ink ring-1 ring-line ring-inset hover:bg-surface-3 active:translate-y-px disabled:opacity-50",
  ghost: "text-ink-2 hover:bg-surface-3 hover:text-ink disabled:opacity-40",
  danger: "bg-critical text-white hover:brightness-110 active:translate-y-px disabled:opacity-50",
};

const sizes: Record<Size, string> = {
  sm: "h-7 gap-1.5 rounded-lg px-2.5 text-xs",
  md: "h-9 gap-2 rounded-[10px] px-3.5 text-[13px]",
  lg: "h-11 gap-2 rounded-xl px-5 text-sm",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
}

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  className,
  children,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cx(
        "inline-flex shrink-0 select-none items-center justify-center font-medium transition-[background-color,color,box-shadow,transform] duration-150 ease-out-quint",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    >
      {icon}
      {children}
    </button>
  );
}
