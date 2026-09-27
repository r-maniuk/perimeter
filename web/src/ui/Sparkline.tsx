import { useId } from "react";

/**
 * A single-series trend line: 2 px stroke, a 10 % wash, an end dot with a surface ring. The
 * current value is always printed next to it, so the line only has to show shape.
 */
export function Sparkline({
  values,
  height = 28,
  color = "var(--accent)",
  label,
}: {
  values: readonly number[];
  height?: number;
  color?: string;
  label: string;
}) {
  const gradient = useId();
  const width = 120;
  if (values.length < 2) {
    return <div style={{ height }} aria-hidden="true" className="rounded bg-surface-3/40" />;
  }
  const max = Math.max(...values);
  const min = Math.min(0, ...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const y = (v: number) => height - 4 - ((v - min) / span) * (height - 8);
  const line = values
    .map((v, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(2)},${y(v).toFixed(2)}`)
    .join("");
  const area = `${line}L${width},${height}L0,${height}Z`;
  const endY = y(values.at(-1) ?? 0);

  return (
    <div className="relative" style={{ height }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="block size-full overflow-visible"
        role="img"
        aria-label={label}
      >
        <defs>
          <linearGradient id={gradient} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.18" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill={`url(#${gradient})`} />
        <path
          d={line}
          fill="none"
          stroke={color}
          strokeWidth="2"
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <span
        aria-hidden="true"
        className="absolute size-2 rounded-full ring-2 ring-[var(--surface-solid)]"
        style={{ right: -4, top: endY - 4, background: color }}
      />
    </div>
  );
}
