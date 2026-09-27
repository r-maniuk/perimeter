import { Slider as RadixSlider } from "radix-ui";

export function Slider({
  label,
  value,
  min,
  max,
  step,
  onValueChange,
  onValueCommit,
  valueText,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onValueChange: (value: number) => void;
  onValueCommit?: (value: number) => void;
  valueText?: string;
}) {
  return (
    <RadixSlider.Root
      className="relative flex h-6 w-full touch-none select-none items-center"
      value={[value]}
      min={min}
      max={max}
      step={step ?? 1}
      onValueChange={([v]) => v !== undefined && onValueChange(v)}
      onValueCommit={([v]) => v !== undefined && onValueCommit?.(v)}
    >
      <RadixSlider.Track className="relative h-1 grow overflow-hidden rounded-full bg-surface-3">
        <RadixSlider.Range className="absolute h-full rounded-full bg-accent" />
      </RadixSlider.Track>
      <RadixSlider.Thumb
        aria-label={label}
        aria-valuetext={valueText}
        className="block size-4 rounded-full bg-white shadow-[0_0_0_1px_rgb(0_0_0/0.08),0_2px_6px_rgb(0_0_0/0.22)] transition-transform duration-150 hover:scale-110 focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2"
      />
    </RadixSlider.Root>
  );
}
