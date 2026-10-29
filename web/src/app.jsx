// ICU quality dashboard UI. Adapted from cag-lab/clif-icu-quality-dashboard (b3cdf29):
// the pages and charts are upstream's; data now come from the API (src/clif_dash/server),
// which filters and aggregates on the server, so the browser never holds the row tables.
// The server may carry several dashboards; routes are #/<dashboard>/<page>/<arg>.
import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { scaleLinear } from "d3-scale";
import { line as d3line, curveStepAfter } from "d3-shape";
import { fmt, status, statusLabel } from "./format.js";
import { Sparkline, RunChart, BarBreakdown, StrataLines, Funnel, Lane, HourMonthHeatmap, useWidth, monthLabel } from "./charts.jsx";
import "./styles.css";

let DASH = null; // id of the dashboard on screen
let META = null; // /api/<dashboard>/meta: decisions, registry, funnels, data quality, filter options
let D = null;    // the dashboard's decisions (config/decisions/platform.yaml + <dashboard>.yaml)
let METRICS = [];
let MBYID = {};
let PRIMARY = null; // the dashboard's primary row table: { name, id_column, unit, unit_plural }

const api = (path) => `/api/${DASH}${path}`;
const cap = (text) => text.charAt(0).toUpperCase() + text.slice(1);
const countOf = (n, table) => `${n} ${n === 1 ? table.unit : table.unit_plural}`;
// Ratios and rates both pool a numerator over a denominator; means and medians report n.
const pooled = (m) => m.kind === "ratio" || m.kind === "rate";
const whole = (v) => (v == null ? 0 : Math.round(v).toLocaleString());
// Observed/expected is shown unless the measure's decisions turn it off (show_expected: false)
const showsExpected = (m) => Boolean(m.oe) && D.metrics[m.id].show_expected !== false;

function setDashboard(id, meta) {
  DASH = id;
  META = meta;
  D = meta.decisions;
  METRICS = meta.metrics;
  MBYID = Object.fromEntries(METRICS.map((m) => [m.id, m]));
  PRIMARY = meta.row_tables[0];
  document.title = meta.dashboard.title;
}

// ----------------------------------------------------------------------------
// Data access
// ----------------------------------------------------------------------------
function query(F, extra = {}) {
  const p = new URLSearchParams();
  if (F.from) p.set("from", F.from);
  if (F.to) p.set("to", F.to);
  for (const [f, , kind] of META.filter_fields) {
    if (kind === "flag") { if (F[f]) p.set(f, F[f]); } else for (const v of F[f] || []) p.append(f, v);
  }
  for (const [k, v] of Object.entries(extra)) if (v != null && v !== "") p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Fetch JSON; keeps the previous data while a new request is in flight. */
function useApi(url) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  useEffect(() => {
    if (!url) return undefined;
    let live = true;
    setState((s) => ({ ...s, loading: true }));
    fetch(url)
      .then((r) => (r.ok ? r.json() : r.text().then((t) => Promise.reject(new Error(`${r.status}: ${t}`)))))
      .then((data) => live && setState({ data, error: null, loading: false }))
      .catch((e) => live && setState({ data: null, error: e.message, loading: false }));
    return () => { live = false; };
  }, [url]);
  return state;
}

function Loading({ state, children }) {
  if (state.error) return <div className="card"><p className="empty">Could not load: {state.error}</p></div>;
  if (!state.data) return <div className="card"><p className="empty">Loading…</p></div>;
  return <div className={state.loading ? "stale" : ""}>{children(state.data)}</div>;
}

// LTVV can be viewed at several thresholds (decisions.yaml: thresholds_selectable).
// The route arg carries the choice, e.g. #/metric/ltvv@7, so links and the back button keep it.
function metricMeta(id, thr, def) {
  const meta = D.metrics[id];
  if (id !== "ltvv" || thr == null) return meta;
  const decisions = meta.decisions.map((d) => (d.id === "threshold_ml_kg"
    ? { ...d, display: `≤ ${thr} mL/kg PBW — selected on this page${thr !== def ? ` (default ≤ ${def}, used on the Overview)` : " (default)"}` }
    : d));
  return {
    ...meta, decisions,
    title: `Low tidal volume ventilation (≤ ${thr} mL/kg PBW)`,
    numerator: `IMV hours with set tidal volume ≤ ${thr} mL/kg predicted body weight`,
  };
}

// ----------------------------------------------------------------------------
// Routing (hash) so the browser back button works between pages
// ----------------------------------------------------------------------------
const hashParts = () => window.location.hash.replace(/^#\/?/, "").split("/").filter((p) => p !== "");

function useRoute() {
  const parse = () => {
    const [, page, arg] = hashParts();
    return { page: page || "overview", arg: arg ? decodeURIComponent(arg) : null };
  };
  const [r, setR] = useState(parse);
  useEffect(() => {
    const f = () => { setR(parse()); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  return r;
}
const route = (page, arg) => `#/${DASH}/${page}${arg ? "/" + encodeURIComponent(arg) : ""}`;
const go = (page, arg) => { window.location.hash = route(page, arg); };

// ----------------------------------------------------------------------------
// Filters
// ----------------------------------------------------------------------------
function MultiSelect({ label, options, value, onChange }) {
  const [open, setOpen] = useState(false);
  const sel = value || [];
  const text = sel.length === 0 ? "All" : sel.length === 1 ? sel[0] : `${sel.length} selected`;
  return (
    <div className="ms">
      <label className="f-lab">{label}</label>
      <button className="ms-btn" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span>{text}</span><span className="chev">▾</span>
      </button>
      {open && (
        <div className="ms-pop" onMouseLeave={() => setOpen(false)}>
          <button className="ms-clear" onClick={() => onChange([])}>All</button>
          {options.map((o) => (
            <label key={o} className="ms-opt">
              <input type="checkbox" checked={sel.includes(o)}
                onChange={(e) => onChange(e.target.checked ? [...sel, o] : sel.filter((x) => x !== o))} />
              {o}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

function FilterRail({ F, setF, nEp, nTot }) {
  const months = META.meta.months;
  const active = Object.entries(F).filter(([, v]) => (Array.isArray(v) ? v.length : v)).length;
  return (
    <aside className="rail" aria-label="Filters">
      <div className="rail-head">
        <h2>Filters</h2>
        {active > 0 && <button className="link" onClick={() => setF({})}>Reset</button>}
      </div>
      <div className="rail-count"><b>{nEp ?? "…"}</b> of {countOf(nTot, PRIMARY)}</div>
      <label className="f-lab">From</label>
      <select value={F.from || ""} onChange={(e) => setF({ ...F, from: e.target.value || null })}>
        <option value="">Earliest ({monthLabel(months[0])})</option>
        {months.map((m) => <option key={m} value={m}>{monthLabel(m)}</option>)}
      </select>
      <label className="f-lab">To</label>
      <select value={F.to || ""} onChange={(e) => setF({ ...F, to: e.target.value || null })}>
        <option value="">Latest ({monthLabel(months[months.length - 1])})</option>
        {months.map((m) => <option key={m} value={m}>{monthLabel(m)}</option>)}
      </select>
      {META.filter_fields.map(([f, lab, kind]) => (kind === "flag" ? (
        <React.Fragment key={f}>
          <label className="f-lab">{lab}</label>
          <select value={F[f] || ""} onChange={(e) => setF({ ...F, [f]: e.target.value || null })}>
            <option value="">All</option><option value="yes">Yes</option><option value="no">No</option>
          </select>
        </React.Fragment>
      ) : (
        <MultiSelect key={f} label={lab} options={META.filter_options[f]} value={F[f]} onChange={(v) => setF({ ...F, [f]: v })} />
      )))}
      <p className="rail-note">Each row is one {PRIMARY.unit}. Filters apply to every page.</p>
    </aside>
  );
}

// ----------------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------------
function StatusChip({ s, meta }) {
  const icon = { good: "✓", warning: "!", critical: "✕", none: "–" }[s];
  return <span className={`chip chip-${s}`}><span className="chip-ic" aria-hidden="true">{icon}</span>{statusLabel(s, meta)}</span>;
}

function downloadCSV(name, rows) {
  if (!rows.length) return;
  const cols = Object.keys(rows[0]);
  const esc = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v);
  const csv = [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = name;
  a.click();
}

// ----------------------------------------------------------------------------
// Overview
// ----------------------------------------------------------------------------
function Overview({ F }) {
  const state = useApi(`${api("/overview")}${query(F)}`);
  return (
    <Loading state={state}>{(data) => (
      <div>
        <div className="page-head">
          <h1>Overview</h1>
          <p className="lede">{META.row_tables.map((t) => countOf(data[`n_${t.name}`], t)).join(" and ")} for the current filters. Click any measure for its run chart, breakdowns and the full list of decisions behind it.</p>
        </div>
        {META.domains.map((dom) => {
          const ms = METRICS.filter((m) => D.metrics[m.id].domain === dom);
          return (
            <section key={dom} className="domain">
              <h2 className="domain-title">{dom}</h2>
              <div className="tiles">
                {ms.map((m) => {
                  const meta = D.metrics[m.id];
                  const { agg: a, series } = data.tiles[m.id];
                  const s = status(a.value, meta);
                  return (
                    <button key={m.id} className="tile" onClick={() => go("metric", m.id)}>
                      <div className="tile-top">
                        <span className="tile-num">{meta.number}</span>
                        <span className="tile-title">{meta.short}</span>
                      </div>
                      <div className="tile-val">{fmt(a.value, meta, undefined, true)}</div>
                      <div className="tile-sub">
                        {pooled(m) ? `${whole(a.num)} / ${whole(a.den)} ${m.denLabel}` : `n ${a.n} ${m.denLabel}`}
                        {showsExpected(m) && a.oe != null && <span> · O/E {a.oe.toFixed(2)}</span>}
                      </div>
                      <div className="tile-foot">
                        <StatusChip s={s} meta={meta} />
                        <Sparkline points={series} domain={meta.unit === "%" ? [0, 100] : null} />
                      </div>
                    </button>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>
    )}</Loading>
  );
}

// ----------------------------------------------------------------------------
// Decisions panel
// ----------------------------------------------------------------------------
function DecisionTable({ rows }) {
  return (
    <table className="dtable">
      <thead><tr><th>Decision</th><th>Choice used</th><th>Alternatives</th><th>Source</th></tr></thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.id}>
            <td className="d-lab">{d.label}</td>
            <td>{d.display}</td>
            <td className="muted">{(d.alternatives || []).length ? d.alternatives.join("; ") : "—"}</td>
            <td className="muted">{d.source}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DecisionsPanel({ meta, agg, m }) {
  const [shared, setShared] = useState(false);
  return (
    <section className="card decisions" aria-labelledby="dec-h">
      <h2 id="dec-h">How this is calculated</h2>
      <div className="numden">
        <div><span className="nd-k">Numerator</span><span>{meta.numerator}</span>{pooled(m) && <b className="nd-v">{whole(agg.num)}</b>}</div>
        <div><span className="nd-k">Denominator</span><span>{meta.denominator}</span>{pooled(m) ? <b className="nd-v">{whole(agg.den)}</b> : <b className="nd-v">n {agg.n}</b>}</div>
        <div><span className="nd-k">Target</span><span>{meta.target != null ? `${meta.target_op || (meta.direction === "lower" ? "≤" : "≥")} ${fmt(meta.target, meta)}` : `None set — add one under this metric in config/decisions/${DASH}.yaml`}</span></div>
      </div>
      <h3>Decisions for this measure</h3>
      <DecisionTable rows={meta.decisions} />
      <button className="link" onClick={() => setShared(!shared)} aria-expanded={shared}>
        {shared ? "Hide" : "Show"} the {D.shared.length} decisions shared by every measure
      </button>
      {shared && <DecisionTable rows={D.shared} />}
      <p className="muted small">Every choice above is read from <code>config/decisions/platform.yaml</code> and <code>config/decisions/{DASH}.yaml</code>; change a value there and rebuild (<code>clif-dash build</code>) to update both the numbers and this panel.</p>
    </section>
  );
}

// ----------------------------------------------------------------------------
// Metric detail
// ----------------------------------------------------------------------------
function MetricPage({ id, F }) {
  const [strat, setStrat] = useState("unit");
  const [view, setView] = useState("bars");
  const state = useApi(`${api(`/metric/${encodeURIComponent(id)}`)}${query(F, { stratum: strat })}`);
  return (
    <Loading state={state}>{(data) => {
      const m = data.metric;
      const thr = data.threshold;
      const def = data.default_threshold;
      const meta = metricMeta(m.id, thr, def);
      const agg = data.agg;
      const series = data.series;
      const control = data.control;
      const strata = data.strata;
      const routeId = data.route_id;
      const f = (v) => fmt(v, meta);
      const s = status(agg.value, meta);
      const ix = METRICS.findIndex((x) => x.id === m.id);
      const stratLabel = META.strata.find((x) => x.id === strat).label;
      return (
        <div>
          <nav className="crumbs"><a href={route("overview")}>Overview</a> / {meta.domain}</nav>
          <div className="page-head metric-head">
            <div>
              <h1><span className="h-num">{meta.number}</span>{meta.title}</h1>
              {thr != null && data.thresholds.length > 1 && (
                <div className="thr-pick" role="group" aria-label="Tidal volume threshold">
                  <span className="thr-lab">Threshold</span>
                  {data.thresholds.map(({ threshold: t, agg: a }) => (
                    <button key={t} className={t === thr ? "on" : ""} aria-pressed={t === thr}
                      onClick={() => go("metric", `${m.id}@${t}`)}>
                      ≤ {t} mL/kg<span className="thr-v">{fmt(a.value, meta)}</span>{t === def && <span className="thr-def">default</span>}
                    </button>
                  ))}
                </div>
              )}
              <div className="headline">
                <span className="big">{f(agg.value)}</span>
                <StatusChip s={s} meta={meta} />
                <span className="muted">
                  {pooled(m) ? `${whole(agg.num)} ${m.kind === "rate" ? "per" : "of"} ${whole(agg.den)} ${m.denLabel}` : `${m.kind} of ${agg.n} ${m.denLabel}${agg.q1 != null ? ` · IQR ${f(agg.q1)}–${f(agg.q3)}` : ""}`}
                </span>
              </div>
              {showsExpected(m) && agg.oe != null && (
                <div className="oe-line">
                  Observed {f(agg.value)} vs expected {f(agg.expected)} · <b>O/E {agg.oe.toFixed(2)}</b>
                  <span className="muted"> ({m.id === "mortality" ? "site-fitted model; compares groups with this site's average" : "CLIF consortium global coefficients"})</span>
                </div>
              )}
              {data.extra.length > 0 && (
                <div className="oe-line">
                  {data.extra.map((a) => (
                    <span key={a.key} className="pill">{a.key === "proned" ? "Ever proned" : `Proned ≤ ${a.key.slice(1)} h`}: <b>{f(a.value)}</b> ({a.num}/{a.den})</span>
                  ))}
                </div>
              )}
              {data.parts.length > 0 && (
                <div className="oe-line">
                  {data.parts.map((a) => <span key={a.key} className="pill">{a.label}: <b>{f(a.value)}</b> ({a.num}/{a.den})</span>)}
                </div>
              )}
            </div>
            <div className="pager">
              {ix > 0 && <button className="btn" onClick={() => go("metric", METRICS[ix - 1].id)}>← {D.metrics[METRICS[ix - 1].id].short}</button>}
              {ix < METRICS.length - 1 && <button className="btn" onClick={() => go("metric", METRICS[ix + 1].id)}>{D.metrics[METRICS[ix + 1].id].short} →</button>}
            </div>
          </div>

          <DecisionsPanel meta={meta} agg={agg} m={m} />

          <section className="card">
            <div className="card-head">
              <h2>By month</h2>
              <button className="btn" onClick={() => downloadCSV(`${routeId.replace("@", "_le")}_by_month.csv`, series.map((p) => ({ month: p.month, value: p.value, numerator: p.num, denominator: p.den, n: p.n })))}>Download CSV</button>
            </div>
            <p className="muted small">
              {control ? `${control.kind}-chart: the shaded bar behind each month is its 3-sigma control limit around the overall ${m.kind === "rate" ? "rate" : "mean"}; red points fall outside it. ` : pooled(m) ? `Control limits are not drawn because ${m.denLabel} are clustered within patients, which would make limits falsely narrow. ` : `Monthly ${m.kind}. `}
             
            </p>
            {series.some((p) => p.value != null) ? (
              <RunChart points={series} meta={meta} control={control} target={meta.target} format={f} />
            ) : <p className="empty">No data for this measure under the current filters.</p>}
          </section>

          <section className="card">
            <div className="card-head">
              <h2>Breakdown</h2>
              <div className="seg">
                <select value={strat} onChange={(e) => setStrat(e.target.value)} aria-label="Group by">
                  {META.strata.map((x) => <option key={x.id} value={x.id}>By {x.label}</option>)}
                </select>
                <div className="toggle" role="tablist">
                  <button className={view === "bars" ? "on" : ""} onClick={() => setView("bars")}>Overall</button>
                  <button className={view === "lines" ? "on" : ""} onClick={() => setView("lines")}>Over time</button>
                  <button className={view === "table" ? "on" : ""} onClick={() => setView("table")}>Table</button>
                </div>
              </div>
            </div>
            {view === "bars" && <BarBreakdown rows={strata} meta={meta} target={meta.target} format={f}
              onPick={(k) => go("patients", `${routeId}|${strat}|${k}`)} />}
            {view === "lines" && (
              <>
                <div className="legend">
                  {data.lines.map((l, i) => <span key={l.key}><i className="swatch" style={{ background: `var(--series-${i + 1})` }} />{l.key}</span>)}
                  {strata.length > 6 && <span className="muted">Showing the 6 largest groups</span>}
                </div>
                <StrataLines series={data.lines} months={data.months} meta={meta} format={f} />
              </>
            )}
            {view === "table" && (
              <table className="dtable num">
                <thead><tr><th>{stratLabel}</th><th>Value</th>{pooled(m) && <th>Numerator</th>}<th>{pooled(m) ? "Denominator" : "n"}</th>{showsExpected(m) && <th>Expected</th>}{showsExpected(m) && <th>O/E</th>}</tr></thead>
                <tbody>{strata.map((r) => (
                  <tr key={r.key}>
                    <td>{r.key}</td><td>{f(r.value)}</td>{pooled(m) && <td>{whole(r.num)}</td>}<td>{pooled(m) ? whole(r.den) : r.n}</td>
                    {showsExpected(m) && <td>{f(r.expected)}</td>}{showsExpected(m) && <td>{r.oe != null ? r.oe.toFixed(2) : "—"}</td>}
                  </tr>))}
                </tbody>
              </table>
            )}
            <p className="muted small">Click a bar to list the {PRIMARY.unit_plural} behind it.</p>
          </section>

          {showsExpected(m) && (
            <section className="card">
              <h2>Observed vs expected by {stratLabel}</h2>
              <p className="muted small">{m.id === "mortality"
                ? "Expected = logistic model fitted on this site's own encounters (age, sex, SOFA in the 24 h before IMV, Charlson index). O/E > 1 means more deaths than this site's case mix predicts."
                : "Expected = CLIF proning study global model (age, sex, BMI, norepinephrine-equivalent category, SOFA, minimum P/F). O/E < 1 means less proning than predicted for these patients."}</p>
              <table className="dtable num">
                <thead><tr><th>Group</th><th>{m.denLabel[0].toUpperCase() + m.denLabel.slice(1)}</th><th>Observed</th><th>Expected</th><th>O/E</th></tr></thead>
                <tbody>{strata.map((r) => (
                  <tr key={r.key}>
                    <td>{r.key}</td><td>{r.den}</td><td>{r.num} ({f(r.value)})</td><td>{r.expectedCount != null ? r.expectedCount.toFixed(1) : "—"} ({f(r.expected)})</td><td><b>{r.oe != null ? r.oe.toFixed(2) : "—"}</b></td>
                  </tr>))}
                </tbody>
              </table>
            </section>
          )}

          <div className="two-col">
            <section className="card">
              <h2>Cohort</h2>
              <p className="muted small">{m.source === "proning" ? "Proning cohort (CLIF proning study definition, ported to Python)." : `${cap(PRIMARY.unit)} cohort shared by every measure built on ${PRIMARY.unit_plural}.`} Counts are for the full data, before the filters on the left.</p>
              <Funnel steps={data.funnel} />
            </section>
            <section className="card">
              <h2>Input completeness</h2>
              <p className="muted small">Share of filtered {PRIMARY.unit_plural} with each input this measure depends on. Low completeness usually explains odd values better than practice does.</p>
              {data.completeness.length ? (
                <table className="dtable num">
                  <tbody>{data.completeness.map(({ key, label, n, total }) => (
                    <tr key={key}><td>{label}</td><td>{total ? Math.round((100 * n) / total) : 0}%</td><td className="muted">{n} / {total}</td></tr>
                  ))}</tbody>
                </table>
              ) : <p className="muted small">This measure depends on no optional input.</p>}
              <p className="small">Provenance: {META.meta.clifpy}; pipeline {META.meta.pipeline_version}; data folder <code>{META.meta.data_folder}</code>, built {META.meta.generated_at}.</p>
              <button className="link" onClick={() => go("patients", `${routeId}|missed|1`)}>List {PRIMARY.unit_plural} {m.missedLabel === "missed" ? "that missed this measure" : `${m.missedLabel} use`} →</button>
            </section>
          </div>
        </div>
      );
    }}</Loading>
  );
}

// ----------------------------------------------------------------------------
// Unit comparison heatmap
// ----------------------------------------------------------------------------
// ICU and hospital comparison: one heat-map page, grouped by either column
const COMPARISONS = {
  units: { endpoint: "/units", title: "ICU comparison", column: "unit", one: "ICU", all: "All ICUs" },
  hospitals: { endpoint: "/hospitals", title: "Hospital comparison", column: "hospital", one: "hospital", all: "All hospitals" },
};

function Comparison({ F, by }) {
  const { endpoint, title, column, one, all } = COMPARISONS[by];
  const state = useApi(`${api(endpoint)}${query(F)}`);
  return (
    <Loading state={state}>{(data) => (
      <div>
        <div className="page-head">
          <h1>{title}</h1>
          <p className="lede">Each cell is the measure for one {one} under the current filters. Icons show target status. Click a cell to see its {PRIMARY.unit_plural}.</p>
        </div>
        <div className="card scroll-x">
          <table className="heat">
            <thead>
              <tr><th>Measure</th><th>{all}</th>{data.groups.map((g) => <th key={g}>{g}</th>)}</tr>
            </thead>
            <tbody>
              {data.rows.map((row) => {
                const meta = D.metrics[row.id];
                return (
                  <tr key={row.id}>
                    <th scope="row"><a href={route("metric", row.id)}><span className="h-num sm">{meta.number}</span>{meta.short}</a></th>
                    <HeatCell a={row.all} meta={meta} />
                    {data.groups.map((g) => (
                      <HeatCell key={g} a={row.cells[g]} meta={meta} onClick={() => go("patients", `${row.id}|${column}|${g}`)} />
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    )}</Loading>
  );
}

function HeatCell({ a, meta, onClick }) {
  const s = status(a.value, meta);
  const icon = { good: "✓", warning: "!", critical: "✕", none: "" }[s];
  return (
    <td className={`hc hc-${a.value == null ? "na" : s}`} onClick={onClick} title={statusLabel(s, meta)}>
      {a.value == null ? <span className="muted">—</span> : (
        <>
          <span className="hc-v">{fmt(a.value, meta, undefined, true)}</span>
          {icon && <span className="hc-ic" aria-label={statusLabel(s, meta)}>{icon}</span>}
          <span className="hc-n">n {a.n}</span>
        </>
      )}
    </td>
  );
}

// ----------------------------------------------------------------------------
// Row list (episodes for the respiratory dashboard)
// ----------------------------------------------------------------------------
const PAGE_SIZE = 100;

// The respiratory list shows chosen columns, some computed from two fields; other
// dashboards list every column of their primary rows.
const RESPIRATORY_LIST = {
  columns: (thr) => [
    ["label_id", "Hospitalization"], ["start", "IMV start"], ["unit", "ICU"], ["imv_days", "IMV days"],
    ["vt_pbw_median", "Median Vt/PBW"], ["ltvv", thr != null ? `LTVV hours (≤ ${thr})` : "LTVV hours"], ["plateau", "Plateau ≤ 30"], ["sat", "SAT days"],
    ["sbt", "SBT days"], ["paralytic_8h", "Paralytic > 8 h days"], ["reint_num", "Reintubated"], ["vfd28", "VFD-28"], ["discharge", "Discharge"],
  ],
  cell: (e, c, ltvvCol) => {
    switch (c) {
      case "ltvv": return e.ltvv_den ? `${Math.round((100 * (e[ltvvCol] ?? e.ltvv_num)) / e.ltvv_den)}% of ${e.ltvv_den} h` : <span className="muted">{e.has_height ? "—" : "no height"}</span>;
      case "plateau": return e.plat_den ? `${e.plat_num}/${e.plat_den}` : "—";
      case "sat": return e.sat_den ? `${e.sat_num}/${e.sat_den}` : "—";
      case "sbt": return e.sbt_den ? `${e.sbt_num}/${e.sbt_den}` : "—";
      case "paralytic_8h": return e.para8_den ? `${e.para8_num}/${e.para8_den}` : "—";
      case "reint_num": return e.ext_den ? (e.reint_num ? "Yes" : "No") : "—";
      default: return e[c] ?? "—";
    }
  },
};
const GENERIC_LIST = {
  columns: (thr, rows) => Object.keys(rows[0] || {}).map((c) => [c, c.replace(/_/g, " ")]),
  cell: (e, c) => (typeof e[c] === "number" && !Number.isInteger(e[c]) ? e[c].toFixed(2) : e[c] ?? "—"),
};
// Stewardship: each row is one encounter's days in one ICU in one month, with days of therapy by agent group
const STEWARDSHIP_LIST = {
  columns: () => [
    ["label_id", "Hospitalization"], ["month", "Month"], ["unit", "ICU"], ["first_day", "First day"], ["last_day", "Last day"],
    ["days_present", "Days present"], ["days_any", "Days on antibacterials"], ["dot_all_antibacterial", "Antibacterial DOT"],
    ["dot_broad_hospital_onset", "Broad, hospital-onset"], ["dot_broad_community", "Broad, community"],
    ["dot_resistant_gram_positive", "Anti-MRSA"], ["dot_narrow_beta_lactam", "Narrow beta-lactam"],
    ["dot_cdi_risk", "C. difficile risk"], ["dot_antifungal_candidiasis", "Antifungal"],
    ["dot_anaerobic", "Anti-anaerobic"], ["days_redundant_anaerobic", "Duplicative anaerobic days"],
  ],
  cell: (e, c) => e[c] ?? "—",
};
// Microbiology: each row is one encounter's days in one ICU in one month, with culture and event counts
const MICROBIOLOGY_LIST = {
  columns: () => [
    ["label_id", "Hospitalization"], ["month", "Month"], ["unit", "ICU"], ["first_day", "First day"], ["last_day", "Last day"],
    ["days_present", "Days present"], ["blood_draws", "Blood cultures"], ["contaminated_draws", "Contaminated"],
    ["urine_draws", "Urine cultures"], ["hob_events", "Hospital-onset bacteremia"], ["cdi_events", "Hospital-onset C. difficile"],
  ],
  cell: (e, c) => e[c] ?? "—",
};
// Fluids: each row is one encounter's ICU time in one unit in one month, with mL by input group
const FLUIDS_LIST = {
  columns: () => [
    ["label_id", "Hospitalization"], ["month", "Month"], ["unit", "ICU"], ["icu_days", "ICU days"],
    ["vol_total", "All input (mL)"], ["vol_iv_fluids", "IV fluids"], ["vol_iv_crystalloid", "Crystalloid"],
    ["vol_iv_colloid", "Colloid"], ["vol_blood_products", "Blood products"], ["vol_nutrition", "Nutrition"],
    ["vol_medication_fluids", "Medication fluids"], ["vol_renal_replacement_fluids", "Renal replacement"],
    ["vol_flush_irrigation", "Flushes"], ["vol_other", "Other"],
  ],
  cell: (e, c) => (e[c] == null ? "—" : c === "icu_days" ? e[c].toFixed(1) : typeof e[c] === "number" && c.startsWith("vol_") ? Math.round(e[c]).toLocaleString() : e[c]),
};
// Flow: each row is one ICU encounter, attributed to its first ICU row
const hours = (v) => (v == null ? "—" : v.toFixed(1));
const yes = (v) => (v ? "Yes" : "—");
const FLOW_LIST = {
  columns: () => [
    ["label_id", "Hospitalization"], ["month", "Month"], ["unit", "ICU"], ["icu_admission", "ICU admission"],
    ["icu_days", "ICU days"], ["ed_to_icu_hours", "ED to ICU (h)"], ["transferred_out", "Transferred out"],
    ["night_transfer_out", "At night"], ["readmitted_48h", "Readmitted ≤ 48 h"], ["died_in_icu", "Died in ICU"],
    ["discharged_from_icu", "Discharged from ICU"],
  ],
  cell: (e, c) => (c === "icu_days" || c === "ed_to_icu_hours" ? hours(e[c]) : typeof e[c] === "number" && c !== "month" ? yes(e[c]) : e[c] ?? "—"),
};
// Glycemic, liberation and kidney: each row is one encounter's days in one ICU in one month, with day counts
const stayList = (counts) => ({
  columns: () => [["label_id", "Hospitalization"], ["month", "Month"], ["unit", "ICU"], ["first_day", "First day"],
    ["last_day", "Last day"], ["days_present", "Days present"], ...counts],
  cell: (e, c) => (e[c] == null ? "—" : typeof e[c] === "number" && !Number.isInteger(e[c]) ? e[c].toFixed(1) : e[c]),
});
const GLYCEMIC_LIST = stayList([
  ["glucose_days", "Days with glucose"], ["glucose_lowest", "Lowest"], ["glucose_highest", "Highest"],
  ["days_severe_hypo", "Days < 40"], ["days_hypo", "Days < 70"], ["days_severe_hyper", "Days > 300"],
  ["days_in_range", "Days mean in range"], ["insulin_days", "Insulin infusion days"], ["insulin_hypo_days", "< 70 on insulin"],
]);
const LIBERATION_LIST = stayList([
  ["pain_days", "Pain assessed"], ["imv_days", "IMV days"], ["sat_eligible_days", "SAT eligible"], ["sat_days", "SAT"],
  ["sbt_days", "SBT"], ["rass_days", "RASS days"], ["rass_in_range", "RASS −2 to +1"], ["rass_readings", "RASS readings"],
  ["deep_days", "Deep days"], ["sedative_days", "Sedative infusion"], ["benzodiazepine_days", "Benzodiazepine"],
  ["cam_days", "CAM done"], ["cam_positive_days", "CAM positive"],
]);
const KIDNEY_LIST = stayList([
  ["creatinine_days", "Creatinine days"], ["aki_events", "AKI onset"], ["severe_aki_events", "Stage 2–3 onset"],
  ["crrt_days", "CRRT days"], ["crrt_dose_mean", "Mean dose (mL/kg/h)"], ["crrt_dose_target_days", "Days ≥ target"],
]);
const LISTS = { respiratory: RESPIRATORY_LIST, stewardship: STEWARDSHIP_LIST, microbiology: MICROBIOLOGY_LIST, fluids: FLUIDS_LIST,
  flow: FLOW_LIST, glycemic: GLYCEMIC_LIST, liberation: LIBERATION_LIST, kidney: KIDNEY_LIST };

// Detail views, by dashboard id: { tab label, component }. A dashboard gets the tab only when
// its server side has a detail builder and the UI has a view registered here.
const DETAILS = { respiratory: { label: "Timeline", hint: " Click a row for its hourly timeline.", view: (props) => <Timeline {...props} /> } };
const detailView = () => (META.dashboard.detail ? DETAILS[DASH] : null);

function Patients({ F, arg }) {
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState(["start", -1]);
  const [offset, setOffset] = useState(0);
  useEffect(() => { const t = setTimeout(() => setSearch(q), 300); return () => clearTimeout(t); }, [q]);
  useEffect(() => { setOffset(0); }, [arg, search, sort, F]);
  const [mid, field, val] = (arg || "").split("|");
  const selection = mid && MBYID[mid.split("@")[0]] ? { metric: mid, field, value: val } : {};
  const [sk, sd] = sort;
  const params = { ...selection, q: search, sort: sk, direction: sd };
  const state = useApi(`${api("/rows")}${query(F, { ...params, offset, limit: PAGE_SIZE })}`);
  const { columns: listColumns, cell } = LISTS[DASH] || GENERIC_LIST;
  const idColumn = PRIMARY.id_column;
  const missedValue = selection.metric && field === "missed" ? `${selection.metric.split("@")[0]}|missed|1` : "";
  return (
    <div>
      <div className="page-head">
        <h1>{cap(PRIMARY.unit_plural)}</h1>
        <p className="lede">One row per {PRIMARY.unit}. Only hospitalization IDs are shown.{detailView() ? detailView().hint : ""}</p>
      </div>
      <div className="card">
        <div className="card-head">
          <div className="seg">
            <input type="search" placeholder="Search ID, ICU, discharge…" value={q} onChange={(e) => setQ(e.target.value)} />
            <select value={missedValue} onChange={(e) => go("patients", e.target.value || null)} aria-label={`Show ${PRIMARY.unit_plural} picked out by a measure`}>
              <option value="">All {PRIMARY.unit_plural}</option>
              {METRICS.map((x) => <option key={x.id} value={`${x.id}|missed|1`}>{cap(x.missedLabel)}: {D.metrics[x.id].short}</option>)}
            </select>
          </div>
          <a className="btn" href={`${api("/rows.csv")}${query(F, params)}`}>Download CSV</a>
        </div>
        <Loading state={state}>{(data) => (
          <>
            {data.note && <p className="filter-note">{data.note} · <a href={route("patients")}>clear</a></p>}
            <div className="scroll-x">
              <table className="dtable list">
                <thead><tr>{listColumns(data.threshold, data.rows).map(([c, lab]) => (
                  <th key={c} onClick={() => setSort([c, sk === c ? -sd : 1])} className="sortable">
                    {lab}{sk === c ? (sd > 0 ? " ▲" : " ▼") : ""}
                  </th>))}</tr></thead>
                <tbody>{data.rows.map((e) => (
                  <tr key={e[idColumn]} onClick={detailView() ? () => go("timeline", e[idColumn]) : undefined}
                    className={detailView() ? "clickable" : ""}>
                    {listColumns(data.threshold, data.rows).map(([c]) => <td key={c}>{cell(e, c, data.numerator_column)}</td>)}
                  </tr>))}
                </tbody>
              </table>
            </div>
            <div className="pager">
              <span className="muted small">
                {data.total ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, data.total)} of ${countOf(data.total, PRIMARY)}` : `0 ${PRIMARY.unit_plural}`}
              </span>
              {offset > 0 && <button className="btn" onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>← Previous {PAGE_SIZE}</button>}
              {offset + PAGE_SIZE < data.total && <button className="btn" onClick={() => setOffset(offset + PAGE_SIZE)}>Next {PAGE_SIZE} →</button>}
            </div>
          </>
        )}</Loading>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------
// Timeline (built by the server for one episode on request)
// ----------------------------------------------------------------------------
const DEV_COLOR = { imv: "var(--series-1)", nippv: "var(--series-2)", cpap: "var(--series-2)", "high flow nc": "var(--series-3)", "trach collar": "var(--series-4)", "nasal cannula": "var(--seq-250)", "face mask": "var(--seq-250)", "room air": "var(--surface-3)" };

function Timeline({ F, arg }) {
  const first = useApi(arg ? null : `${api("/rows")}${query(F, { limit: 1 })}`);
  useEffect(() => {
    if (!arg && first.data && first.data.rows.length) go("timeline", first.data.rows[0].episode_id);
  }, [arg, first.data]);
  const state = useApi(arg ? api(`/detail/${encodeURIComponent(arg)}`) : null);
  const [ref, w] = useWidth(900);
  if (!arg) return (
    <div><div className="page-head"><h1>Timeline</h1></div>
      <div className="card"><p className="empty">{first.data && !first.data.rows.length ? "No episodes under the current filters." : "Pick an episode from the Episodes page."}</p></div></div>
  );
  return (
    <Loading state={state}>{(data) => {
      const e = data.row;
      const T = data.series;
      const pr = data.proning;
      const iw = w - 92;
      const t0 = Math.min(-6, ...T.t), t1 = Math.max(T.end_h + 6, ...T.t);
      const x = scaleLinear().domain([t0, t1]).range([0, iw]);
      const ticks = x.ticks(Math.max(4, Math.floor(iw / 90)));
      const xAxis = (h) => ticks.map((t) => <g key={t}><line x1={x(t)} x2={x(t)} y1={0} y2={h} className="grid" /></g>);
      const segs = [];
      T.device.forEach((d, i) => {
        if (i === 0 || d !== T.device[i - 1]) segs.push({ d, s: T.t[i], e: T.t[i + 1] ?? T.t[i] + 1 });
        else segs[segs.length - 1].e = T.t[i + 1] ?? T.t[i] + 1;
      });
      const lineOf = (vals, y) => d3line().defined((v) => v != null).x((_, i) => x(T.t[i])).y((v) => y(v))(vals);
      const yVt = scaleLinear().domain([4, 12]).range([60, 0]);
      const yPeep = scaleLinear().domain([0, 20]).range([50, 0]);
      const yFio2 = scaleLinear().domain([0.2, 1]).range([50, 0]);
      const yPf = scaleLinear().domain([0, 500]).range([50, 0]);
      const yR = scaleLinear().domain([-5, 4]).range([60, 0]);
      const neeMax = Math.max(0.1, ...T.nee.map((p) => p[1]));
      const yN = scaleLinear().domain([0, neeMax]).nice().range([40, 0]);
      const paraThr = ((D.metrics.paralytic_8h || {}).decisions || []).find((d) => d.id === "hours_threshold")?.value ?? 8;
      return (
        <div>
          <nav className="crumbs"><a href={route("patients")}>Episodes</a> / {e.label_id}</nav>
          <div className="page-head metric-head">
            <div>
              <h1>Hospitalization {e.label_id}</h1>
              <p className="lede">{e.unit} · IMV {e.start} → {e.end} ({e.imv_days} days) · {e.sex}, {e.age_band} · {e.code_status} · discharge: {e.discharge}{e.pbw ? ` · PBW ${e.pbw} kg` : " · no height recorded"}</p>
            </div>
          </div>
          <div className="two-col tight">
            <div className="card mini"><b>{e.ltvv_den ? `${Math.round((100 * e.ltvv_num) / e.ltvv_den)}%` : "—"}</b><span>LTVV hours</span></div>
            <div className="card mini"><b>{e.sat_den ? `${e.sat_num}/${e.sat_den}` : "—"}</b><span>SAT days</span></div>
            <div className="card mini"><b>{e.para8_den ? `${e.para8_num}/${e.para8_den}` : "—"}</b><span>Paralytic &gt; {paraThr} h days</span></div>
            <div className="card mini"><b>{e.sbt_den ? `${e.sbt_num}/${e.sbt_den}` : "—"}</b><span>SBT days</span></div>
            <div className="card mini"><b>{e.vfd28}</b><span>VFD-28</span></div>
            <div className="card mini"><b>{pr ? (pr.proned ? `${pr.hours_to_prone} h` : "Not proned") : "Not in cohort"}</b><span>Proning</span></div>
          </div>
          <section className="card" ref={ref}>
            <p className="muted small">Hours from IMV start (0). Each lane has its own scale; values come from the clifpy waterfall (forward-filled within mode blocks), so flat stretches mean "no new setting documented".</p>
            <div className="xaxis" style={{ marginLeft: 76, width: iw }}>
              {ticks.map((t) => <span key={t} style={{ left: x(t) }}>{t} h</span>)}
            </div>
            <Lane title="Device" w={w} height={30}>
              {segs.filter((sg) => sg.d).map((sg, i) => (
                <rect key={i} x={x(sg.s)} y={2} width={Math.max(1, x(sg.e) - x(sg.s) - 1)} height={18} rx="3"
                  fill={DEV_COLOR[sg.d] || "var(--surface-3)"}><title>{sg.d} {sg.s}–{sg.e} h</title></rect>
              ))}
            </Lane>
            <div className="legend small">
              {["imv", "nippv", "high flow nc", "trach collar", "nasal cannula"].map((d) => <span key={d}><i className="swatch" style={{ background: DEV_COLOR[d] }} />{d === "imv" ? "IMV" : d}</span>)}
            </div>
            <Lane title="Set tidal volume ÷ PBW" unit="mL/kg; dashed = 8" w={w} height={70} y={yVt} yTicks={[4, 6, 8, 10, 12]}>
              {xAxis(60)}
              <line x1={0} x2={iw} y1={yVt(8)} y2={yVt(8)} className="target-line" />
              <path d={lineOf(T.vt_pbw.map((v) => (v == null ? null : Math.min(12, Math.max(4, v)))), yVt)} fill="none" stroke="var(--series-1)" strokeWidth="2" />
            </Lane>
            <Lane title="PEEP" unit="cmH2O" w={w} height={60} y={yPeep} yTicks={[0, 10, 20]}>
              {xAxis(50)}
              <path d={lineOf(T.peep, yPeep)} fill="none" stroke="var(--series-1)" strokeWidth="2" />
            </Lane>
            <Lane title="FiO2" w={w} height={60} y={yFio2} yTicks={[0.2, 0.6, 1]}>
              {xAxis(50)}
              <path d={lineOf(T.fio2, yFio2)} fill="none" stroke="var(--series-1)" strokeWidth="2" />
            </Lane>
            <Lane title="P/F ratio (ABG)" unit="dashed = 150" w={w} height={60} y={yPf} yTicks={[0, 150, 300, 500]}>
              {xAxis(50)}
              <line x1={0} x2={iw} y1={yPf(150)} y2={yPf(150)} className="target-line" />
              {T.pf.map(([t, v], i) => <circle key={i} cx={x(t)} cy={yPf(Math.min(500, v))} r="4" fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth="1.5"><title>P/F {v} at {t} h</title></circle>)}
            </Lane>
            <Lane title="Plateau pressure" unit="cmH2O; dashed = 30" w={w} height={60} y={scaleLinear().domain([0, 40]).range([50, 0])} yTicks={[0, 20, 40]}>
              {xAxis(50)}
              <line x1={0} x2={iw} y1={50 - 50 * 30 / 40} y2={50 - 50 * 30 / 40} className="target-line" />
              {T.plateau.map(([t, v], i) => <circle key={i} cx={x(t)} cy={50 - 50 * Math.min(40, v) / 40} r="4" fill={v > 30 ? "var(--status-critical)" : "var(--series-1)"} stroke="var(--surface-1)" strokeWidth="1.5"><title>Plateau {v} at {t} h</title></circle>)}
            </Lane>
            <Lane title="RASS" unit="band = −2 to +1" w={w} height={70} y={yR} yTicks={[-5, -2, 0, 1, 4]}>
              {xAxis(60)}
              <rect x={0} y={yR(1)} width={iw} height={yR(-2) - yR(1)} className="target-band" />
              {T.rass.map(([t, v], i) => <circle key={i} cx={x(t)} cy={yR(v)} r="4" fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth="1.5"><title>RASS {v} at {t} h</title></circle>)}
            </Lane>
            <Lane title="Infusions, proning, trials" w={w} height={110}>
              {xAxis(104)}
              {[["sedative", "Sedative", 4], ["paralytic", "Paralytic", 24], ["prone", "Prone", 64]].map(([k, lab, yy]) => (
                <g key={k}>
                  <text x={-6} y={yy + 8} dy="0.32em" textAnchor="end" className="tick">{lab}</text>
                  {(T.bands[k] || []).map(([a, b], i) => <rect key={i} x={x(a)} y={yy} width={Math.max(2, x(b) - x(a))} height={14} rx="3" className={`band-${k}`}><title>{lab} {a}–{b} h</title></rect>)}
                </g>
              ))}
              <text x={-6} y={52} dy="0.32em" textAnchor="end" className="tick">Para &gt; {paraThr} h</text>
              {(T.para_days || []).map(([a, b, h, flag], i) => {
                const x0 = Math.max(0, x(a)) + 1, x1 = Math.min(iw, x(b)) - 1;
                if (x1 <= x0) return null;
                return (
                  <g key={i}>
                    <rect x={x0} y={44} width={x1 - x0} height={16} rx="3" className={flag ? "para-day para-day-flag" : "para-day"}>
                      <title>{`Intubated ICU day ${i + 1}: ${h} h of paralytic infusion${flag ? ` (> ${paraThr} h — counts toward measure 8b)` : ""}`}</title>
                    </rect>
                    {x1 - x0 > 34 && h > 0 && <text x={(x0 + x1) / 2} y={52} dy="0.32em" textAnchor="middle" className={flag ? "para-day-lab flag" : "para-day-lab"}>{h} h</text>}
                  </g>
                );
              })}
              <text x={-6} y={92} dy="0.32em" textAnchor="end" className="tick">Trials</text>
              {(() => { let last = -1e9; return T.events.map(([t, k, v], i) => {
                const show = x(t) - last > 64; if (show) last = x(t);
                return (
                  <g key={i}><line x1={x(t)} x2={x(t)} y1={84} y2={100} stroke={k === "SAT" ? "var(--series-1)" : "var(--series-4)"} strokeWidth="2"><title>{k} {v} at {t} h</title></line>
                    {show && <text x={x(t) + 3} y={92} dy="0.32em" className="tick">{k} {v}</text>}</g>
                ); }); })()}
            </Lane>
            <p className="lane-note">Para &gt; {paraThr} h row: one box per intubated ICU day. A filled red box means more than {paraThr} h of paralytic infusion that day (counts against measure 8b); outlined boxes are intubated ICU days under {paraThr} h.</p>
            <Lane title="Vasopressors (norepinephrine equivalent)" unit="mcg/kg/min" w={w} height={50} y={yN} yTicks={yN.ticks(2)}>
              {xAxis(40)}
              {T.nee.length > 0 && <path d={d3line().curve(curveStepAfter).x((p) => x(p[0])).y((p) => yN(p[1]))(T.nee)} fill="none" stroke="var(--series-1)" strokeWidth="2" />}
            </Lane>
          </section>
          <div className="pager">
            {data.previous && <button className="btn" onClick={() => go("timeline", data.previous)}>← Previous episode</button>}
            {data.next && <button className="btn" onClick={() => go("timeline", data.next)}>Next episode →</button>}
          </div>
        </div>
      );
    }}</Loading>
  );
}

// ----------------------------------------------------------------------------
// Time of day: when events happen, by hour (rows) and month (columns)
// ----------------------------------------------------------------------------
function HeatmapCard({ heatmap, F, share }) {
  const state = useApi(`${api(`/heatmap/${heatmap.id}`)}${query(F)}`);
  return (
    <section className="card">
      <div className="card-head"><h2>{heatmap.title}</h2></div>
      {heatmap.note && <p className="muted">{heatmap.note}</p>}
      <Loading state={state}>{(data) => data.n
        ? <>{data.heatmap.unfiltered.length > 0 && <p className="muted">Not filtered by {data.heatmap.unfiltered.join(", ").toLowerCase()}: this heatmap is built without them.</p>}
          <HourMonthHeatmap data={data} share={share} event={heatmap.event} unit={heatmap.unit} floor={heatmap.floor} /></>
        : <p className="empty">No {heatmap.event} under the current filters.</p>}</Loading>
    </section>
  );
}

function TimeOfDay({ F }) {
  const [share, setShare] = useState(true);
  return (
    <div>
      <div className="page-head">
        <h1>Time of day</h1>
        <p className="lede">Each column is a month and each row an hour of the day, in site time. For counted events, "% of month"
          shows what share of that month's events fell in each hour, so busy and quiet months compare fairly, and "Count" shows
          the numbers. Rates and shares of time are shown as they are. The bars on the right cover every month shown.</p>
      </div>
      {META.heatmaps.some((h) => h.kind === "events") && <div className="seg" style={{ marginBottom: 12 }}>
        <div className="toggle" role="tablist">
          <button className={share ? "on" : ""} onClick={() => setShare(true)}>% of month</button>
          <button className={share ? "" : "on"} onClick={() => setShare(false)}>Count</button>
        </div>
      </div>}
      {META.heatmaps.map((h) => <HeatmapCard key={h.id} heatmap={h} F={F} share={share} />)}
    </div>
  );
}

// ----------------------------------------------------------------------------
// Data quality
// ----------------------------------------------------------------------------
function Quality({ F }) {
  const state = useApi(`${api("/quality")}${query(F)}`);
  const dq = META.data_quality;
  return (
    <Loading state={state}>{(data) => {
      const notes = [];
      if (dq.sat_documented_rows === 0) notes.push("No documented SAT assessments (sat_delivery_pass_fail): the SAT measure relies on the EHR phenotype only.");
      if (dq.prone_position_rows < 5) notes.push(`Only ${dq.prone_position_rows} prone position row(s) in the position table: proning measures will be near zero or empty.`);
      if ((dq.semi_prone_names || []).length) notes.push(`Position names mapped to prone that contain "semi": ${dq.semi_prone_names.join(", ")}. Review the mCIDE mapping.`);
      if (dq.respiratory_rows_without_time) notes.push(`${dq.respiratory_rows_without_time.toLocaleString()} respiratory_support rows have no recorded_dttm and were left out of the ventilator timeline.`);
      if (dq.icu_rows_without_location_type) notes.push(`${dq.icu_rows_without_location_type} ICU ADT rows have no location_type: they appear as "ICU (type not mapped)".`);
      if (dq.months_counted) {
        const left = dq.months_counted;
        const outside = left.icu_days_left_out != null ? `${left.icu_days_left_out.toLocaleString()} ICU days` : `${(left.icu_encounters_left_out ?? 0).toLocaleString()} ICU encounters admitted`;
        notes.push(`Months counted: ${left.first} to ${left.last}, the whole months inside both this dashboard's source records and the span of admissions. ${outside} outside them are left out, because values there would read falsely low (nothing recorded yet) or skewed (only long-staying patients left).`);
      }
      const clif30 = Object.entries(dq.respiratory_rows_clif30_only || {});
      if (clif30.length) notes.push(`respiratory_support rows with CLIF 3.0 values that CLIF 2.1, and so clifpy's waterfall, has no name for: ${clif30.map(([v, n]) => `${v} (${n.toLocaleString()})`).join(", ")}. A t_piece device is read as IMV, with mode Blow by when none is charted (clifpy's 2.1 convention for a T-piece trial), so it does not end the IMV episode; check that this site charts only intubated or tracheostomized patients on a T-piece that way. An APRV or AVAPS mode on an IMV device still counts as IMV.`);
      if (dq.deaths_counted === 0 && dq.discharge_categories) notes.push(`No encounter in the cohort is counted as a death, so mortality reads 0%. A death is a discharge_category listed under the mortality measure's death decision in config/decisions/respiratory.yaml (Expired, in any case or spacing). This cohort's discharge categories: ${Object.entries(dq.discharge_categories).map(([v, n]) => `${v} (${n.toLocaleString()})`).join(", ")}. If deaths are recorded under another value, add it to that decision exactly as recorded and rebuild.`);
      const unconverted = Object.entries(dq.dose_conversion || {}).filter(([k]) => !k.endsWith(" success"));
      if (unconverted.length) notes.push(`Infusion rows whose rate clifpy could not convert to the Time of day page's unit (they count as running, but not toward a mean rate): ${unconverted.map(([k, n]) => `${k} (${n.toLocaleString()})`).join(", ")}. Check med_dose_unit for these drugs, and that weights are charted for weight-based conversions.`);
      if (dq.documented_sat_rows === 0) notes.push("No documented SAT rows (sat_delivery_pass_fail or sat_delivery_performed): the SAT measure reads 0% because nothing is charted, not because no trials were given. The respiratory dashboard also finds SATs from the EHR.");
      if (dq.documented_sbt_rows === 0) notes.push("No documented SBT rows (sbt_delivery_pass_fail or sbt_delivery_performed): the SBT measure reads 0% because nothing is charted. The respiratory dashboard also finds SBTs from the EHR.");
      if (dq.cam_values && Object.keys(dq.cam_values).length) notes.push(`CAM-ICU values recorded: ${Object.entries(dq.cam_values).map(([v, n]) => `${v} (${n.toLocaleString()})`).join(", ")}. Positive is the value "positive", or 1 with no text value; check that this site's values map that way.`);
      const units = (counts, label) => { const e = Object.entries(counts || {}); if (e.length && e.some(([u]) => !/^mg\/dl$/i.test(u))) notes.push(`${label} rows by reference_unit: ${e.map(([u, n]) => `${u} (${n.toLocaleString()})`).join(", ")}. The thresholds assume mg/dL; convert other units before building.`); };
      units(dq.glucose_rows_by_unit, "Glucose");
      units(dq.creatinine_rows_by_unit, "Creatinine");
      if (dq.glucose_rows_outside_range) notes.push(`${dq.glucose_rows_outside_range.toLocaleString()} glucose results are outside clifpy's plausible range and are not counted.`);
      if (dq.creatinine_rows_outside_range) notes.push(`${dq.creatinine_rows_outside_range.toLocaleString()} creatinine results are 0 or outside clifpy's plausible range and are not counted.`);
      if (dq.crrt_days_without_dose) notes.push(`${dq.crrt_days_without_dose.toLocaleString()} CRRT days have no flow rates or no recorded weight, so they are left out of the CRRT dose measure.`);
      if (dq.icu_encounters_with_negative_ed_time) notes.push(`${dq.icu_encounters_with_negative_ed_time.toLocaleString()} encounters have an ED row starting after their first ICU row, so they have no ED-to-ICU time.`);
      if (dq.input_rows_without_positive_volume) notes.push(`${dq.input_rows_without_positive_volume.toLocaleString()} input rows have a missing, zero or negative volume and are not counted.`);
      const overLimit = Object.entries(dq.input_rows_over_limit || {});
      if (overLimit.length) notes.push(`${overLimit.reduce((n, [, v]) => n + v.rows, 0).toLocaleString()} input rows are above their group's volume limit and are not counted, as likely entry or unit errors (${overLimit.map(([g, v]) => `${g.replaceAll("_", " ")}: ${v.rows.toLocaleString()} rows, ${Math.round(v.litres).toLocaleString()} L`).join("; ")}).`);
      if (dq.icu_rows_without_end) notes.push(`${dq.icu_rows_without_end.toLocaleString()} ICU ADT rows have no out time and no discharge time, so they add no days present.`);
      if (dq.antimicrobial_rows_without_time) notes.push(`${dq.antimicrobial_rows_without_time.toLocaleString()} antimicrobial administrations have no admin_dttm and are not counted.`);
      if (dq.unmapped_mar_action_rows) notes.push(`${dq.unmapped_mar_action_rows.toLocaleString()} intermittent medication rows have an unmapped MAR action (mar_action_group "other") and are not counted as doses.`);
      if (data.n_rows && data.n_with_height != null && data.n_with_height / data.n_rows < 0.9) notes.push(`Height is missing for ${data.n_rows - data.n_with_height} of ${data.n_rows} episodes, so their ventilator hours drop out of the LTVV denominator.`);
      return (
        <div>
          <div className="page-head">
            <h1>Data quality</h1>
            <p className="lede">What the data can and cannot support, so gaps are not mistaken for practice.</p>
          </div>
          {notes.length > 0 && (
            <section className="card callout">
              <h2>Things to know</h2>
              <ul>{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
            </section>
          )}
          {data.completeness.length > 0 && <section className="card scroll-x">
            <h2>Input completeness by ICU</h2>
            <table className="dtable num">
              <thead><tr><th>Input</th><th>All</th>{data.units.map((u) => <th key={u}>{u}</th>)}</tr></thead>
              <tbody>{data.completeness.map((row) => (
                <tr key={row.key}><td>{row.label}</td>
                  {["__all__", ...data.units].map((u) => {
                    const p = row.cells[u];
                    return <td key={u} className={p != null && p < 80 ? "warn-cell" : ""}>{p == null ? "—" : `${p}%`}</td>;
                  })}
                </tr>))}
              </tbody>
            </table>
          </section>}
          <div className="two-col">
            {dq.antimicrobial_administrations && (
              <section className="card">
                <h2>Antimicrobial administrations</h2>
                <table className="dtable num"><tbody>
                  <tr><td>Staged (counted agents, adult encounters with ICU time)</td><td>{dq.antimicrobial_administrations.staged.toLocaleString()}</td></tr>
                  <tr><td>Administered, with a time</td><td>{dq.antimicrobial_administrations.administered.toLocaleString()}</td></tr>
                  <tr><td>By a counted route</td><td>{dq.antimicrobial_administrations.counted_route.toLocaleString()}</td></tr>
                  <tr><td>Given in an ICU</td><td>{dq.antimicrobial_administrations.in_icu.toLocaleString()}</td></tr>
                </tbody></table>
                <p className="muted small">Rows by route, all counted agents: {Object.entries(dq.antimicrobial_rows_by_route || {}).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(", ") || "none"}.</p>
              </section>
            )}
            <section className="card">
              <h2>Tables loaded</h2>
              <table className="dtable num"><tbody>{Object.entries(dq.row_counts).map(([k, v]) => <tr key={k}><td><code>{k}</code></td><td>{v.toLocaleString()} rows</td>{dq.outliers && <td className="muted">{(dq.outliers[k] ?? Object.entries(dq.outliers).filter(([kk]) => kk.startsWith(k + ".")).reduce((s, [, n]) => s + n, 0)) || 0} outliers set to missing</td>}</tr>)}</tbody></table>
              {dq.outliers && <p className="muted small">Outlier counts cover the rows the build read: respiratory support for adult encounters with any row that could be IMV, the other tables for cohort encounters.</p>}
            </section>
            {("mortality_model" in dq || "proning_model" in dq) && <section className="card">
              <h2>Models</h2>
              {dq.mortality_model ? (
                <>
                  <h3>Expected mortality (site-fitted)</h3>
                  <table className="dtable num"><tbody>{dq.mortality_model.terms.map((t, i) => <tr key={t}><td>{t}</td><td>{dq.mortality_model.coef[i]}</td></tr>)}</tbody></table>
                  <p className="muted small">Fitted on {dq.mortality_model.n} encounters with {dq.mortality_model.events} deaths (ridge-penalised logistic regression). Small samples give unstable O/E.</p>
                </>
              ) : <p className="muted small">Not enough encounters or deaths to fit the mortality model.</p>}
              {dq.proning_model && dq.proning_model.coefficients && (
                <>
                  <h3>Expected proning (CLIF global coefficients)</h3>
                  <table className="dtable num"><tbody>{Object.entries(dq.proning_model.coefficients).map(([k, v]) => <tr key={k}><td>{k.replace("coef_", "")}</td><td>{v}</td></tr>)}</tbody></table>
                </>
              )}
              <p className="small">Built with {META.meta.clifpy}. Proning logic: {META.meta.proning_source}.</p>
            </section>}
          </div>
        </div>
      );
    }}</Loading>
  );
}

// ----------------------------------------------------------------------------
// Shell
// ----------------------------------------------------------------------------
function App({ dashboards }) {
  const route_ = useRoute();
  const [F, setF] = useState({});
  const [theme, setTheme] = useState(null);
  const count = useApi(`${api("/rows")}${query(F, { limit: 0 })}`);
  useEffect(() => { if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme; }, [theme]);
  const tabs = [["overview", "Overview"], ["metric", "Measures"], ["units", "ICU comparison"],
    ...((META.filter_options.hospital || []).length > 1 ? [["hospitals", "Hospital comparison"]] : []), ["patients", cap(PRIMARY.unit_plural)],
    ...(detailView() ? [["timeline", detailView().label]] : []),
    ...((META.heatmaps || []).length ? [["timeofday", "Time of day"]] : []), ["quality", "Data quality"]];
  const firstMetric = METRICS[0].id;
  return (
    <div className="shell viz-root">
      <header className="top">
        <div className="brand">
          <div className="logo" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22"><path d="M12 3v7M12 10c-2 0-6 1-7 6s1 5 3 5 4-3 4-6M12 10c2 0 6 1 7 6s-1 5-3 5-4-3-4-6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
          </div>
          <div>
            <div className="title">{META.dashboard.title}</div>
            <div className="sub">{META.meta.site_name} · built {META.meta.generated_at} · CLIF
              {dashboards.length > 1 && <> · <a href="#/">All dashboards</a></>}
            </div>
          </div>
        </div>
        <nav className="tabs" aria-label="Pages">
          {tabs.map(([k, lab]) => (
            <a key={k} href={route(k)} className={route_.page === k ? "on" : ""}>{lab}</a>
          ))}
        </nav>
        <button className="btn theme" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle dark mode">
          {theme === "dark" ? "☀" : "☾"}
        </button>
      </header>
      <div className="body">
        <FilterRail F={F} setF={setF} nEp={count.data ? count.data.total : null} nTot={META.n_total} />
        <main>
          {route_.page === "overview" && <Overview F={F} />}
          {route_.page === "metric" && <MetricPage id={route_.arg || firstMetric} F={F} key={(route_.arg || firstMetric).split("@")[0]} />}
          {(route_.page === "units" || route_.page === "hospitals") && <Comparison F={F} by={route_.page} key={route_.page} />}
          {route_.page === "patients" && <Patients F={F} arg={route_.arg} />}
          {route_.page === "timeline" && detailView() && detailView().view({ F, arg: route_.arg })}
          {route_.page === "timeofday" && (META.heatmaps || []).length > 0 && <TimeOfDay F={F} />}
          {route_.page === "quality" && <Quality F={F} />}
        </main>
      </div>
      <footer className="foot">Shows patient-level data — run this server only inside your institution. Measures computed from CLIF tables with clifpy; definitions in config/decisions/.<div className="copyright">Interface and metric logic © 2026 Cathy Gao-Howard on behalf of the CLIF Consortium</div></footer>
    </div>
  );
}

// ----------------------------------------------------------------------------
// Dashboards: pick one from the hash, load its meta, and remount on a switch
// ----------------------------------------------------------------------------
/** The dashboard the hash names. A hash from before dashboards had ids (#/metric/ltvv)
 *  gets the first dashboard's id put in front of it; an empty hash with several
 *  dashboards shows the landing page (null). */
function dashboardFromHash(ids) {
  const parts = hashParts();
  if (ids.includes(parts[0])) return parts[0];
  if (parts.length === 0 && ids.length > 1) return null;
  window.history.replaceState(null, "", `#/${[ids[0], ...parts].join("/")}`);
  return ids[0];
}

function Landing({ dashboards }) {
  return (
    <div className="shell viz-root">
      <main className="landing">
        <div className="page-head"><h1>CLIF ICU quality dashboards</h1></div>
        <div className="tiles">
          {dashboards.map((d) => <a key={d.id} className="tile" href={`#/${d.id}/overview`}><span className="tile-title">{d.title}</span></a>)}
        </div>
      </main>
    </div>
  );
}

function Root({ dashboards }) {
  const ids = dashboards.map((d) => d.id);
  const [dash, setDash] = useState(() => dashboardFromHash(ids));
  const [loaded, setLoaded] = useState({ id: null, error: null });
  useEffect(() => {
    const f = () => setDash(dashboardFromHash(ids));
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  useEffect(() => {
    if (!dash) { document.title = "CLIF ICU quality dashboards"; return undefined; }
    let live = true;
    fetch(`/api/${dash}/meta`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((meta) => { if (live) { setDashboard(dash, meta); setLoaded({ id: dash, error: null }); } })
      .catch((e) => live && setLoaded({ id: null, error: e.message }));
    return () => { live = false; };
  }, [dash]);
  if (!dash) return <Landing dashboards={dashboards} />;
  if (loaded.error) return <p style={{ padding: "2rem" }}>Could not load the {dash} dashboard ({loaded.error}).</p>;
  if (loaded.id !== dash) return <p style={{ padding: "2rem" }}>Loading…</p>;
  return <App key={dash} dashboards={dashboards} />;
}

const root = document.getElementById("root");
fetch("/api/dashboards")
  .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
  .then((dashboards) => {
    if (!dashboards.length) return Promise.reject(new Error("no dashboard has been built"));
    createRoot(root).render(<Root dashboards={dashboards} />);
  })
  .catch((e) => {
    root.innerHTML = `<p style='padding:2rem;font-family:system-ui'>Could not reach the dashboard API (${e.message}). Start it with <code>uv run clif-dash serve --config &lt;site.yaml&gt;</code>.</p>`;
  });
