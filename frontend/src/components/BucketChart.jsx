import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatBucketLabel, formatPct } from "../format";
import "./BucketChart.css";

// Grouped bar: recovery rate % by bucket, agent vs baseline (PRD M8
// section 2). Two named series -> categorical color (identity), agent =
// slot 1 blue, baseline = slot 2 orange -- same pair used everywhere
// else in the dashboard so the reader learns the mapping once. <=24px
// bars, 4px rounded data-end, a 2px surface gap between adjacent bars
// (Bar's own `radius` + a hairline `stroke` in the surface color stands
// in for the gap here, since Recharts draws bars edge-to-edge by
// default).
function TooltipContent({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bucket-chart__tooltip">
      <div className="bucket-chart__tooltip-title">{formatBucketLabel(label)}</div>
      {payload.map((entry) => (
        <div key={entry.dataKey} className="bucket-chart__tooltip-row">
          <span className="bucket-chart__tooltip-key" style={{ background: entry.color }} />
          <span className="bucket-chart__tooltip-name">{entry.name}</span>
          <span className="bucket-chart__tooltip-value">{formatPct(entry.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function BucketChart({ byBucket }) {
  if (!byBucket?.length) return null;

  const data = byBucket.map((row) => ({
    bucket: row.bucket,
    label: formatBucketLabel(row.bucket),
    agent: row.agent?.recovery_rate_pct ?? 0,
    baseline: row.baseline?.recovery_rate_pct ?? 0,
    agentN: row.agent?.n ?? 0,
    baselineN: row.baseline?.n ?? 0,
  }));

  return (
    <section className="card">
      <h2 className="card__title">Recovery rate by bucket</h2>
      <p className="card__subtitle">Agent vs. baseline, this run — same events, agent's own classification</p>
      <div className="bucket-chart">
        <ResponsiveContainer width="100%" height={340}>
          <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 8 }} barGap={2} barCategoryGap="20%">
            <CartesianGrid vertical={false} stroke="var(--gridline)" />
            <XAxis
              dataKey="label"
              tick={{ fill: "var(--text-secondary)", fontSize: 12 }}
              axisLine={{ stroke: "var(--baseline-axis)" }}
              tickLine={false}
              interval={0}
            />
            <YAxis
              tick={{ fill: "var(--text-muted)", fontSize: 12 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={(v) => `${v}%`}
              width={40}
            />
            <Tooltip content={<TooltipContent />} cursor={{ fill: "var(--surface-2)" }} />
            <Legend
              iconType="square"
              wrapperStyle={{ fontSize: 13, color: "var(--text-secondary)" }}
              formatter={(value) => <span style={{ color: "var(--text-secondary)" }}>{value}</span>}
            />
            <Bar dataKey="agent" name="Agent" fill="var(--series-agent)" radius={[4, 4, 0, 0]} maxBarSize={24} />
            <Bar
              dataKey="baseline"
              name="Baseline"
              fill="var(--series-baseline)"
              radius={[4, 4, 0, 0]}
              maxBarSize={24}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
