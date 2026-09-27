import type { ReactNode } from "react";

export function Empty({
  icon,
  title,
  children,
  action,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center px-6 py-10 text-center">
      <div className="mb-3 flex size-11 items-center justify-center rounded-2xl bg-accent-soft text-accent">
        {icon}
      </div>
      <p className="font-semibold text-[13px] text-ink">{title}</p>
      {children && (
        <p className="mt-1 max-w-[30ch] text-muted text-xs leading-relaxed">{children}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
