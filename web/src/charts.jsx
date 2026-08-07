import React, { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

/** Recharts animates on mount; honour the OS setting instead of overriding it.
    Lives here rather than in App.jsx: charts are its only consumer, and importing
    it from App created an App <-> charts import cycle. */
function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(
    () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false
  );
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = (e) => setReduced(e.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}

// Roles, not raw hex — light/dark swap in styles.css only.
const css = (name) => `var(${name})`;
export const OUTCOMES = [
  // Order matters: green and red are never adjacent in a stack.
  { key: "offer", label: "Offer", color: css("--outcome-offer") },
  { key: "active", label: "Active", color: css("--outcome-active") },
  { key: "ghosted", label: "No reply", color: css("--outcome-ghosted") },
  { key: "rejected", label: "Rejected", color: css("--outcome-rejected") },
];

const AXIS = { fontSize: 11, fill: css("--text-muted") };
const gridProps = { stroke: css("--grid"), strokeDasharray: "0", vertical: false };

export function Legend({ items }) {
  return (
    <div className="legend">
      {items.map((i) => (
        <span key={i.label}>
          <i className="swatch" style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  );
}

/** Recharts sizes LabelList text to the bar's own width, so short bars wrap their value
    onto two lines and collide. Render a plain <text> instead — no wrapping logic. */
const valueLabel = (format) => (props) => {
  const { x, y, width, height, value } = props;
  if (value == null) return null;
  return (
    <text
      x={x + width + 8}
      y={y + height / 2}
      dominantBaseline="central"
      style={{ fill: css("--text-secondary"), fontSize: 11 }}
    >
      {format(value)}
    </text>
  );
};

function TT({ active, payload, label, unit = "" }) {
  if (!active || !payload?.length) return null;
  const rows = payload.filter((p) => p.value > 0);
  if (!rows.length) return null;
  return (
    <div className="tt">
      <div className="tt-title">{label}</div>
      {rows.map((p) => (
        <div className="tt-row" key={p.dataKey}>
          <i className="swatch" style={{ background: p.color || p.fill }} />
          {p.name}: <b>{p.value}{unit}</b>
        </div>
      ))}
    </div>
  );
}

/** Funnel — one measure across ordered stages, so a single hue; bar length is the encoding. */
export function Funnel({ data, total }) {
  const still = usePrefersReducedMotion();
  const rows = data.map((d) => ({
    ...d,
    label: d.stage.replace(/_/g, " "),
    pct: total ? Math.round((100 * d.count) / total) : 0,
  }));
  return (
    <ResponsiveContainer width="100%" height={230}>
      <BarChart data={rows} layout="vertical" margin={{ left: 4, right: 78, top: 4, bottom: 4 }}>
        <CartesianGrid {...gridProps} horizontal={false} />
        <XAxis type="number" tick={AXIS} axisLine={false} tickLine={false} />
        <YAxis
          type="category"
          dataKey="label"
          width={116}
          tick={AXIS}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip content={<TT />} cursor={{ fill: css("--grid"), opacity: 0.35 }} />
        <Bar
          dataKey="count"
          name="Applications"
          fill={css("--series-1")}
          radius={[0, 4, 4, 0]}
          barSize={18}
          isAnimationActive={!still}
        >
          <LabelList
            dataKey="count"
            content={valueLabel((v) => `${v}  ·  ${total ? Math.round((100 * v) / total) : 0}%`)}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Applications per month, stacked by outcome. 2px surface gap between segments. */
export function Monthly({ data }) {
  const still = usePrefersReducedMotion();
  return (
    <ResponsiveContainer width="100%" height={230}>
      <BarChart data={data} margin={{ left: -14, right: 8, top: 4, bottom: 4 }}>
        <CartesianGrid {...gridProps} />
        <XAxis dataKey="month" tick={AXIS} axisLine={false} tickLine={false} />
        <YAxis tick={AXIS} axisLine={false} tickLine={false} allowDecimals={false} />
        <Tooltip content={<TT />} cursor={{ fill: css("--grid"), opacity: 0.35 }} />
        {OUTCOMES.map((o, i) => (
          <Bar
            key={o.key}
            dataKey={o.key}
            name={o.label}
            stackId="s"
            fill={o.color}
            stroke={css("--surface-1")}
            strokeWidth={2}
            radius={i === 0 ? [4, 4, 0, 0] : 0}
            maxBarSize={46}
            isAnimationActive={!still}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Response rate per source — outbound only, single measure, single hue. */
export function BySource({ data }) {
  const still = usePrefersReducedMotion();
  // Carry n on the axis label: a 100% rate off 2 sends must not read like a better
  // source than 60% off 10.
  const rows = data
    .filter((d) => d.total > 0)
    .map((d) => ({ ...d, label: `${d.source} (${d.total})` }));
  return (
    <ResponsiveContainer width="100%" height={Math.max(150, rows.length * 42)}>
      <BarChart data={rows} layout="vertical" margin={{ left: 4, right: 56, top: 4, bottom: 4 }}>
        <CartesianGrid {...gridProps} horizontal={false} />
        <XAxis type="number" domain={[0, 100]} unit="%" tick={AXIS} axisLine={false} tickLine={false} />
        <YAxis
          type="category"
          dataKey="label"
          width={104}
          tick={AXIS}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip content={<TT unit="%" />} cursor={{ fill: css("--grid"), opacity: 0.35 }} />
        <Bar
          dataKey="response_rate"
          name="Response rate"
          radius={[0, 4, 4, 0]}
          barSize={18}
          isAnimationActive={!still}
        >
          {rows.map((r) => (
            <Cell
              key={r.source}
              fill={css("--series-1")}
              // Under 3 sends the rate is noise — mute it so it doesn't read as a finding.
              fillOpacity={r.total < 3 ? 0.4 : 1}
            />
          ))}
          <LabelList dataKey="response_rate" content={valueLabel((v) => `${v}%`)} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
