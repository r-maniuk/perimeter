import type { ReactNode } from "react";

export function SectionLabel({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="flex items-center justify-between px-1 pt-4 pb-2">
      <h3 className="font-semibold text-[11px] text-muted uppercase tracking-[0.08em]">
        {children}
      </h3>
      {aside}
    </div>
  );
}
