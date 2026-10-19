import type { AnalyticsSeriesPoint } from "@/lib/api";

// Inline SVG sparkline from hourly request counts — a faithful port of the
// vanilla app.js sparkline() (W=160, H=28, polyline, normalized to max).
export function Sparkline({ series }: { series: AnalyticsSeriesPoint[] }) {
  if (!series || series.length === 0) {
    return (
      <span className="text-xs text-muted-foreground">no traffic yet</span>
    );
  }
  const vals = series.map((p) => p.count);
  const max = Math.max(...vals) || 1;
  const W = 160;
  const H = 28;
  const n = vals.length;
  const step = n > 1 ? W / (n - 1) : W;
  const pts = vals
    .map((v, i) => {
      const x = (i * step).toFixed(1);
      const y = (H - (v / max) * (H - 3) - 1).toFixed(1);
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className="text-primary"
    >
      <polyline
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        points={pts}
      />
    </svg>
  );
}
