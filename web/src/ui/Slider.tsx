import { Slider as RadixSlider } from "radix-ui";
import { type KeyboardEvent, useRef } from "react";

/** Keys that step the value, and which way (the slider runs left to right). */
const STEP_KEYS: Record<string, 1 | -1> = {
  ArrowRight: 1,
  ArrowUp: 1,
  PageUp: 1,
  ArrowLeft: -1,
  ArrowDown: -1,
  PageDown: -1,
};

export function Slider({
  label,
  value,
  min,
  max,
  step,
  keyStep,
  onValueChange,
  onValueCommit,
  valueText,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  /**
   * Where an arrow or page key takes the value from `value`, for a scale whose steps are not
   * even (Page keys and Shift take a large step). The keyboard then works like a drag:
   * `onValueChange` on every press, `onValueCommit` once, when the key is released.
   */
  keyStep?: (value: number, direction: 1 | -1, large: boolean) => number;
  onValueChange: (value: number) => void;
  onValueCommit?: (value: number) => void;
  valueText?: string;
}) {
  /** Where the keyboard has taken the value, until the key is released. */
  const stepped = useRef<number | null>(null);

  function onKeyDown(event: KeyboardEvent) {
    const direction = STEP_KEYS[event.key];
    if (!keyStep || direction === undefined) return;
    // Radix would step evenly along the track, as `step` says.
    event.preventDefault();
    const from = stepped.current ?? value;
    const large = event.key.startsWith("Page") || event.shiftKey;
    const next = Math.min(max, Math.max(min, keyStep(from, direction, large)));
    if (next === from) return;
    stepped.current = next;
    onValueChange(next);
  }

  function commitStep() {
    const next = stepped.current;
    stepped.current = null;
    if (next !== null) onValueCommit?.(next);
  }

  return (
    <RadixSlider.Root
      className="relative flex h-6 w-full touch-none select-none items-center"
      value={[value]}
      min={min}
      max={max}
      step={step ?? 1}
      onValueChange={([v]) => v !== undefined && onValueChange(v)}
      onValueCommit={([v]) => v !== undefined && onValueCommit?.(v)}
      onKeyDown={onKeyDown}
      onKeyUp={(event) => {
        if (STEP_KEYS[event.key] !== undefined) commitStep();
      }}
      onBlur={commitStep}
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
