import React, { useMemo, useRef, useState, useLayoutEffect } from "react";
import { scaleLinear, scaleBand, scalePoint } from "d3-scale";
import { tickLabel } from "./format.js";
import { line as d3line, area as d3area, curveMonotoneX } from "d3-shape";

export function useWidth(initial = 640) {
  const ref = useRef(null);
  const [w, setW] = useState(initial);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, Math.floor(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const monthLabel = (m) => {
  const [y, mo] = m.split("-");
  return `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+mo - 1]} ${y.slice(2)}`;
};
export { monthLabel };

function Tooltip({ x, y, w, children }) {
  if (x == null) return null;
  const left = Math.min(Math.max(x + 12, 4), w - 190);
  return (
    <div className="viz-tooltip" style={{ left, top: Math.max(0, y - 10) }}>
      {children}
    </div>
  );
}

/** Sparkline for KPI tiles */
export function Sparkline({ points, width = 120, height = 32, domain }) {
  const vals = points.map((p) => p.value).filter((v) => v != null);
  if (vals.length < 2) return <svg width={width} height={height} aria-hidden="true" />;
  const x = scaleLinear().domain([0, points.length - 1]).range([3, width - 3]);
  const lo = domain ? domain[0] : Math.min(...vals);
  const hi = domain ? domain[1] : Math.max(...vals);
  const y = scaleLinear().domain(lo === hi ? [lo - 1, hi + 1] : [lo, hi]).range([height - 4, 4]);
  const path = d3line()
    .defined((p) => p.value != null)
    .x((_, i) => x(i))
    .y((p) => y(p.value))
    .curve(curveMonotoneX)(points);
  const last = [...points].reverse().find((p) => p.value != null);
  const li = points.lastIndexOf(last);
  return (
    <svg width={width} height={height} aria-hidden="true">
      <path d={path} fill="none" stroke="var(--series-1)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(li)} cy={y(last.value)} r="3" fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth="1.5" />
    </svg>
  );
}

/** Monthly run / control chart. points: [{month, value, num, den, n}] */
export function RunChart({ points, meta, control, target, height = 280, format }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState(null);
  const m = { t: 16, r: 92, b: 34, l: 48 };
  const iw = w - m.l - m.r, ih = height - m.t - m.b;
  const x = scalePoint().domain(points.map((p) => p.month)).range([0, iw]).padding(0.5);
  const vals = points.flatMap((p) => (p.value != null ? [p.value] : []));
  const lims = control ? control.limits.filter(Boolean).flatMap((l) => [l.lo, l.hi]) : [];
  const pct = meta.unit === "%";
  let lo = Math.min(...vals, ...(target != null ? [target] : []), ...lims);
  let hi = Math.max(...vals, ...(target != null ? [target] : []), ...lims);
  if (pct) { lo = Math.max(0, Math.floor((lo - 5) / 10) * 10); hi = Math.min(100, Math.ceil((hi + 5) / 10) * 10); }
  else { const pad = (hi - lo) * 0.15 || 1; lo = Math.max(0, lo - pad); hi = hi + pad; }
  if (!isFinite(lo)) { lo = 0; hi = pct ? 100 : 1; }
  const y = scaleLinear().domain([lo, hi]).nice(5).range([ih, 0]);
  const path = d3line().defined((p) => p.value != null).x((p) => x(p.month)).y((p) => y(p.value))(points);
  // per-month control limits drawn as a light range bar behind each point
  const bw = Math.min(28, Math.max(6, x.step() * 0.6));
  const band = control
    ? points.map((p, i) => {
        const l = control.limits[i];
        if (!l || p.value == null) return null;
        return <rect key={p.month} x={x(p.month) - bw / 2} y={y(l.hi)} width={bw} height={Math.max(1, y(l.lo) - y(l.hi))} rx="3" className="control-band" />;
      })
    : null;
  const step = Math.ceil(points.length / Math.max(1, Math.floor(iw / 64)));
  const onMove = (ev) => {
    const r = ev.currentTarget.getBoundingClientRect();
    const px = ev.clientX - r.left - m.l;
    let best = null, bd = 1e9;
    points.forEach((p, i) => { const d = Math.abs(x(p.month) - px); if (d < bd) { bd = d; best = i; } });
    setHover(best);
  };
  const hp = hover != null ? points[hover] : null;
  const out = (p, i) => control && control.limits[i] && p.value != null && (p.value > control.limits[i].hi || p.value < control.limits[i].lo);
  return (
    <div ref={ref} className="viz-wrap" style={{ height }}>
      <svg width={w} height={height} role="img" aria-label={`${meta.title} by month`} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <g transform={`translate(${m.l},${m.t})`}>
          {y.ticks(5).map((t) => (
            <g key={t}>
              <line x1={0} x2={iw} y1={y(t)} y2={y(t)} className="grid" />
              <text x={-8} y={y(t)} dy="0.32em" textAnchor="end" className="tick">{pct ? `${t}%` : tickLabel(t)}</text>
            </g>
          ))}
          {points.map((p, i) => i % step === 0 && (
            <text key={p.month} x={x(p.month)} y={ih + 22} textAnchor="middle" className="tick">{monthLabel(p.month)}</text>
          ))}
          {band}
          {control && (
            <g>
              <line x1={0} x2={iw} y1={y(control.center)} y2={y(control.center)} className="center-line" />
              <text x={iw + 6} y={y(control.center)} dy="0.32em" className="ref-label">Mean {format(control.center)}</text>
            </g>
          )}
          {target != null && (
            <g>
              <line x1={0} x2={iw} y1={y(target)} y2={y(target)} className="target-line" />
              <text x={iw + 6} y={y(target)} dy="0.32em" className="ref-label">Target {format(target)}</text>
            </g>
          )}
          <path d={path} fill="none" stroke="var(--series-1)" strokeWidth="2" strokeLinejoin="round" />
          {points.map((p, i) => p.value != null && (
            <circle key={p.month} cx={x(p.month)} cy={y(p.value)} r={out(p, i) ? 6 : 4.5}
              className="pt"
              fill={out(p, i) ? "var(--status-critical)" : "var(--series-1)"} />
          ))}
          {hp && <line x1={x(hp.month)} x2={x(hp.month)} y1={0} y2={ih} className="crosshair" />}
        </g>
      </svg>
      {hp && (
        <Tooltip x={m.l + x(hp.month)} y={hp.value != null ? m.t + y(hp.value) : m.t} w={w}>
          <div className="tt-title">{monthLabel(hp.month)}</div>
          <div className="tt-row"><span>{meta.short}</span><b>{format(hp.value)}</b></div>
          {hp.den != null && meta.unit === "%" && <div className="tt-row"><span>Count</span><b>{hp.num} / {hp.den}</b></div>}
          {meta.unit !== "%" && <div className="tt-row"><span>n</span><b>{hp.n}</b></div>}
          {control && control.limits[hover] && <div className="tt-row"><span>Control limits</span><b>{format(control.limits[hover].lo)}–{format(control.limits[hover].hi)}</b></div>}
          {out(hp, hover) && <div className="tt-warn">Outside control limits (special-cause signal)</div>}
        </Tooltip>
      )}
    </div>
  );
}

/** Horizontal bars by stratum. rows: [{key, value, n, num, den}] */
export function BarBreakdown({ rows, meta, target, format, onPick }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState(null);
  const rowH = 30;
  const m = { t: 8, r: 120, b: 26, l: 170 };
  const height = m.t + m.b + rows.length * rowH;
  const iw = w - m.l - m.r;
  const pct = meta.unit === "%";
  const max = pct ? 100 : Math.max(...rows.map((r) => r.value ?? 0), target ?? 0) * 1.1 || 1;
  const x = scaleLinear().domain([0, max]).nice().range([0, iw]);
  const y = scaleBand().domain(rows.map((r) => r.key)).range([0, rows.length * rowH]).paddingInner(0.3);
  return (
    <div ref={ref} className="viz-wrap" style={{ height }}>
      <svg width={w} height={height} role="img" aria-label={`${meta.title} by group`}>
        <g transform={`translate(${m.l},${m.t})`}>
          {x.ticks(5).map((t) => (
            <g key={t}>
              <line x1={x(t)} x2={x(t)} y1={0} y2={rows.length * rowH} className="grid" />
              <text x={x(t)} y={rows.length * rowH + 18} textAnchor="middle" className="tick">{pct ? `${t}%` : tickLabel(t)}</text>
            </g>
          ))}
          {rows.map((r) => {
            const bw = r.value != null ? Math.max(2, x(r.value)) : 0;
            const y0 = y(r.key), bh = y.bandwidth();
            return (
              <g key={r.key} onMouseEnter={() => setHover(r.key)} onMouseLeave={() => setHover(null)}
                 onClick={() => onPick && onPick(r.key)} style={{ cursor: onPick ? "pointer" : "default" }}>
                <rect x={-m.l} y={y0 - 4} width={w} height={bh + 8} fill="transparent" />
                <text x={-10} y={y0 + bh / 2} dy="0.32em" textAnchor="end" className="cat-label">{r.key}</text>
                {r.value != null && (
                  <path d={`M0,${y0} h${bw - 4} a4,4 0 0 1 4,4 v${bh - 8} a4,4 0 0 1 -4,4 h${-(bw - 4)} z`}
                    className="bar" opacity={hover && hover !== r.key ? 0.55 : 1} />
                )}
                <text x={bw + 8} y={y0 + bh / 2} dy="0.32em" className="val-label">
                  {format(r.value)}<tspan className="muted"> · n {r.n}</tspan>
                </text>
              </g>
            );
          })}
          {target != null && (
            <g>
              <line x1={x(target)} x2={x(target)} y1={-4} y2={rows.length * rowH} className="target-line" />
            </g>
          )}
        </g>
      </svg>
    </div>
  );
}

/** Multi-series monthly lines for a stratifier (≤ 6 series, fixed color order) */
export function StrataLines({ series, months, meta, format, height = 240 }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState(null);
  const m = { t: 12, r: 16, b: 34, l: 48 };
  const iw = w - m.l - m.r, ih = height - m.t - m.b;
  const x = scalePoint().domain(months).range([0, iw]).padding(0.5);
  const vals = series.flatMap((s) => s.points.filter((p) => p.value != null).map((p) => p.value));
  const pct = meta.unit === "%";
  const y = scaleLinear().domain(pct ? [0, 100] : [0, Math.max(1, ...vals) * 1.1]).nice(5).range([ih, 0]);
  const step = Math.ceil(months.length / Math.max(1, Math.floor(iw / 64)));
  const onMove = (ev) => {
    const r = ev.currentTarget.getBoundingClientRect();
    const px = ev.clientX - r.left - m.l;
    let best = null, bd = 1e9;
    months.forEach((mo, i) => { const d = Math.abs(x(mo) - px); if (d < bd) { bd = d; best = i; } });
    setHover(best);
  };
  return (
    <div ref={ref} className="viz-wrap" style={{ height }}>
      <svg width={w} height={height} role="img" aria-label="By group over time" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <g transform={`translate(${m.l},${m.t})`}>
          {y.ticks(5).map((t) => (
            <g key={t}>
              <line x1={0} x2={iw} y1={y(t)} y2={y(t)} className="grid" />
              <text x={-8} y={y(t)} dy="0.32em" textAnchor="end" className="tick">{pct ? `${t}%` : tickLabel(t)}</text>
            </g>
          ))}
          {months.map((mo, i) => i % step === 0 && (
            <text key={mo} x={x(mo)} y={ih + 22} textAnchor="middle" className="tick">{monthLabel(mo)}</text>
          ))}
          {series.map((s, si) => {
            const d = d3line().defined((p) => p.value != null).x((p) => x(p.month)).y((p) => y(p.value))(s.points);
            return (
              <g key={s.key}>
                <path d={d} fill="none" stroke={`var(--series-${si + 1})`} strokeWidth="2" strokeLinejoin="round" />
                {s.points.map((p) => p.value != null && (
                  <circle key={p.month} cx={x(p.month)} cy={y(p.value)} r="4" fill={`var(--series-${si + 1})`} stroke="var(--surface-1)" strokeWidth="2" />
                ))}
              </g>
            );
          })}
          {hover != null && <line x1={x(months[hover])} x2={x(months[hover])} y1={0} y2={ih} className="crosshair" />}
        </g>
      </svg>
      {hover != null && (
        <Tooltip x={m.l + x(months[hover])} y={m.t + 10} w={w}>
          <div className="tt-title">{monthLabel(months[hover])}</div>
          {series.map((s, si) => {
            const p = s.points[hover];
            return (
              <div className="tt-row" key={s.key}>
                <span><i className="swatch" style={{ background: `var(--series-${si + 1})` }} />{s.key}</span>
                <b>{p && p.value != null ? `${format(p.value)} (n ${p.n ?? p.den})` : "—"}</b>
              </div>
            );
          })}
        </Tooltip>
      )}
    </div>
  );
}

/** Cohort funnel: steps with signed counts */
export function Funnel({ steps }) {
  const rows = steps.map((s) => ({ ...s, excl: s.n < 0 || /^(Excluded|Did not|Outside)/.test(s.step) }));
  const max = Math.max(...rows.map((r) => (r.n > 0 ? r.n : 0)), 1);
  return (
    <ol className="funnel">
      {rows.map((r, i) => (
        <li key={i} className={r.excl ? "excl" : "keep"}>
          <span className="f-label">{r.step}</span>
          {!r.excl ? (
            <span className="f-bar-wrap">
              <span className="f-bar" style={{ width: `${(100 * r.n) / max}%` }} />
              <span className="f-n">{r.n.toLocaleString()}{r.unit ? ` ${r.unit}` : ""}</span>
            </span>
          ) : (
            <span className="f-excl">{r.n ? "−" : ""}{Math.abs(r.n).toLocaleString()}{r.unit ? ` ${r.unit}` : ""}</span>
          )}
        </li>
      ))}
    </ol>
  );
}

/** Timeline lane: shared x (hours from IMV start) */
export function Lane({ title, x, w, height = 70, children, yTicks, y, unit }) {
  return (
    <div className="lane">
      <div className="lane-title">{title}{unit ? <span className="muted"> ({unit})</span> : null}</div>
      <svg width={w} height={height}>
        <g transform="translate(76,6)">
          {yTicks && yTicks.map((t) => (
            <g key={t}>
              <line x1={0} x2={w - 92} y1={y(t)} y2={y(t)} className="grid" />
              <text x={-6} y={y(t)} dy="0.32em" textAnchor="end" className="tick">{t}</text>
            </g>
          ))}
          {children}
        </g>
      </svg>
    </div>
  );
}

// Viridis, sampled at nine even stops (matplotlib), for continuous values
const VIRIDIS = ["#440154", "#472d7b", "#3b528b", "#2c728e", "#21918c", "#28ae80", "#5ec962", "#addc30", "#fde725"];
export function viridis(t) {
  const x = Math.min(1, Math.max(0, t)) * (VIRIDIS.length - 1);
  const i = Math.min(VIRIDIS.length - 2, Math.floor(x));
  const mix = (a, b) => Math.round(a + (b - a) * (x - i));
  const rgb = (hex) => [1, 3, 5].map((k) => parseInt(hex.slice(k, k + 2), 16));
  const [a, b] = [rgb(VIRIDIS[i]), rgb(VIRIDIS[i + 1])];
  return `rgb(${mix(a[0], b[0])}, ${mix(a[1], b[1])}, ${mix(a[2], b[2])})`;
}

const hourLabel = (h) => `${String(h).padStart(2, "0")}:00`;

/** By hour of day (rows) and month (columns). Events: counts, or with `share` each month's percent.
    Values (data.values): a rate or share per cell, in `unit`, with the hours behind it (data.weights).
    The bars on the right sum or average every month shown. */
export function HourMonthHeatmap({ data, share, event, unit, floor = 0 }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState(null);
  const isValues = data.values != null;
  const { months, n } = data;
  const counts = isValues ? data.values : data.counts;
  const totals = data.totals || [];
  const m = { t: 20, r: 70, b: 30, l: 46 };
  const cell = 13;
  const iw = Math.max(40, w - m.l - m.r), ih = cell * 24, height = ih + m.t + m.b;
  const x = scaleBand().domain(months).range([0, iw]);
  const value = (h, j) => (isValues ? counts[h][j] : share ? (totals[j] ? (100 * counts[h][j]) / totals[j] : null) : counts[h][j]);
  // The top of the colour scale is the 98th percentile of the cells, so a few sparse months cannot wash out the rest
  const cells = counts.flatMap((row, h) => row.map((_, j) => value(h, j))).filter((v) => v != null).sort((a, b) => a - b);
  const max = cells.length ? cells[Math.min(cells.length - 1, Math.floor(0.98 * (cells.length - 1)))] || cells[cells.length - 1] : 0;
  const capped = cells.length && max < cells[cells.length - 1];
  // A heatmap without a floor (mean RASS, which is negative) starts its colours at the 2nd percentile instead of 0
  const min = floor == null && cells.length ? cells[Math.floor(0.02 * (cells.length - 1))] : 0;
  const floored = floor == null && cells.length && min > cells[0];
  const colour = (v) => viridis(max > min ? (v - min) / (max - min) : 0);
  const hourTotals = isValues ? data.hour_values.map((v) => v ?? 0) : counts.map((row) => row.reduce((a, b) => a + b, 0));
  // The all-months bars are drawn from the colour scale's start, so negative means still show
  const hourMax = Math.max(isValues ? 1e-9 : 1, ...hourTotals.map((t) => t - (floor == null ? Math.min(0, ...hourTotals) : 0)));
  const barLength = (t) => (52 * (t - (floor == null ? Math.min(0, ...hourTotals) : 0))) / hourMax;
  const step = Math.ceil(months.length / Math.max(1, Math.floor(iw / 64)));
  const fmtValue = (v) => (v == null ? "—" : isValues ? `${v.toLocaleString(undefined, { maximumFractionDigits: v < 10 ? 2 : 1 })} ${unit}`
    : share ? `${v.toFixed(1)}%` : v.toLocaleString());
  const hours = (v) => `${v.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${event}`;
  const onMove = (ev) => {
    const r = ev.currentTarget.getBoundingClientRect();
    const px = ev.clientX - r.left - m.l, py = ev.clientY - r.top - m.t;
    const j = Math.floor(px / x.step()), h = Math.floor(py / cell);
    setHover(j >= 0 && j < months.length && h >= 0 && h < 24 ? { h, j, x: ev.clientX - r.left, y: ev.clientY - r.top } : null);
  };
  return (
    <div ref={ref} className="viz-wrap" style={{ height: height + 34 }}>
      <svg width={w} height={height} role="img" aria-label={`${event} by hour of day and month`} onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}>
        <g transform={`translate(${m.l},${m.t})`}>
          {counts.map((row, h) => row.map((_, j) => {
            const v = value(h, j);
            return <rect key={`${h}-${j}`} x={x(months[j])} y={h * cell} width={Math.max(1, x.bandwidth() - 0.5)}
              height={cell - 0.5} fill={v == null ? "var(--surface-2)" : colour(v)} />;
          }))}
          {[0, 3, 6, 9, 12, 15, 18, 21].map((h) => (
            <text key={h} x={-6} y={h * cell + cell / 2} dy="0.32em" textAnchor="end" className="tick">{hourLabel(h)}</text>
          ))}
          {months.map((mo, j) => j % step === 0 && (
            <text key={mo} x={x(mo) + x.bandwidth() / 2} y={ih + 18} textAnchor="middle" className="tick">{monthLabel(mo)}</text>
          ))}
          {hourTotals.map((t, h) => (
            <rect key={h} x={iw + 8} y={h * cell + 1} height={cell - 2} width={Math.max(0, barLength(t))} className="hour-bar" />
          ))}
          <text x={iw + 8} y={-8} className="tick">All months</text>
          {hover && <rect x={x(months[hover.j])} y={hover.h * cell} width={x.bandwidth()} height={cell} className="cell-focus" />}
        </g>
      </svg>
      <div className="heat-legend">
        <span>{isValues ? `${floored ? "≤ " : ""}${fmtValue(min)}` : share ? "0%" : "0"}</span>
        <i style={{ background: `linear-gradient(to right, ${VIRIDIS.join(", ")})` }} />
        <span>{capped ? "≥ " : ""}{fmtValue(max)}{isValues ? "" : share ? " of the month" : ` ${event}`}</span>
        <span className="muted">{isValues ? hours(n) : `${n.toLocaleString()} ${event}`} in all</span>
      </div>
      {hover && (
        <Tooltip x={hover.x} y={hover.y} w={w}>
          <b>{monthLabel(months[hover.j])} · {hourLabel(hover.h)}–{String(hover.h).padStart(2, "0")}:59</b>
          {isValues ? <>
            <div>{fmtValue(counts[hover.h][hover.j])} · {hours(data.weights[hover.h][hover.j])}</div>
            <div className="muted">This hour, all months: {fmtValue(data.hour_values[hover.h])}</div>
          </> : <>
            <div>{counts[hover.h][hover.j].toLocaleString()} {event}
              {totals[hover.j] ? ` · ${((100 * counts[hover.h][hover.j]) / totals[hover.j]).toFixed(1)}% of ${totals[hover.j].toLocaleString()} that month` : ""}</div>
            <div className="muted">This hour, all months: {hourTotals[hover.h].toLocaleString()} ({n ? ((100 * hourTotals[hover.h]) / n).toFixed(1) : 0}%)</div>
          </>}
        </Tooltip>
      )}
    </div>
  );
}
