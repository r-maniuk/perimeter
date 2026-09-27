import { Switch as RadixSwitch } from "radix-ui";
import { useId } from "react";

export function Switch({
  label,
  description,
  checked,
  onCheckedChange,
  disabled,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <label htmlFor={id} className="min-w-0 cursor-pointer">
        <span className="block font-medium text-[13px] text-ink">{label}</span>
        {description && <span className="block text-muted text-xs">{description}</span>}
      </label>
      <RadixSwitch.Root
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled ?? false}
        className="relative h-[22px] w-[38px] shrink-0 rounded-full bg-surface-3 ring-1 ring-line-strong ring-inset transition-colors duration-200 disabled:opacity-50 data-[state=checked]:bg-accent data-[state=checked]:ring-transparent"
      >
        <RadixSwitch.Thumb className="block size-[18px] translate-x-[2px] rounded-full bg-white shadow-[0_1px_3px_rgb(0_0_0/0.25)] transition-transform duration-200 ease-spring data-[state=checked]:translate-x-[18px]" />
      </RadixSwitch.Root>
    </div>
  );
}
