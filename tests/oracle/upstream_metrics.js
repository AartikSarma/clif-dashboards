// Metric registry: how each metric aggregates the episode / proning rows the
// pipeline exported. Definitions and decisions come from decisions.yaml (DATA.decisions).

const ratio = (num, den) => ({ kind: "ratio", num, den });
const mean = (field) => ({ kind: "mean", field });
const median = (field) => ({ kind: "median", field });

export const METRICS = [
  { id: "ltvv", source: "episodes", agg: ratio("ltvv_num", "ltvv_den"), denLabel: "IMV hours", missed: (e) => e.ltvv_den > 0 && e.ltvv_num / e.ltvv_den < 0.8, needs: ["has_height", "has_vt"] },
  { id: "vt_first24", source: "episodes", agg: mean("vt24"), denLabel: "episodes", missed: (e) => e.vt24 != null && e.vt24 > 8, needs: ["has_height", "has_vt"] },
  { id: "plateau", source: "episodes", agg: ratio("plat_num", "plat_den"), denLabel: "readings", missed: (e) => e.plat_den > 0 && e.plat_num < e.plat_den, needs: ["has_plateau"] },
  { id: "height_doc", source: "episodes", agg: ratio("hdoc_num", "hdoc_den"), denLabel: "episodes", missed: (e) => e.hdoc_den > 0 && !e.hdoc_num, needs: ["has_height"] },
  { id: "prone_12h", source: "proning", agg: ratio("p12", null), denLabel: "patients", oe: "p12_expected", missed: (p) => !p.p12, needs: ["has_abg"] },
  { id: "prone_any", source: "proning", agg: ratio("p24", null), denLabel: "patients", extra: ["p72", "proned"], missed: (p) => !p.p24, needs: ["has_abg"] },
  { id: "prone_dose", source: "proning", agg: median("median_session_h"), denLabel: "proned patients", missed: (p) => p.median_session_h != null && p.median_session_h < 16, needs: [] },
  { id: "sat", source: "episodes", agg: ratio("sat_num", "sat_den"), denLabel: "vent-days", parts: [["sat_doc", "Documented"], ["sat_pheno", "EHR phenotype"]], missed: (e) => e.sat_den > 0 && e.sat_num < e.sat_den, needs: ["has_rass"] },
  { id: "sbt", source: "episodes", agg: ratio("sbt_num", "sbt_den"), denLabel: "vent-days", parts: [["sbt_doc", "Documented"], ["sbt_pheno", "EHR phenotype"]], missed: (e) => e.sbt_den > 0 && e.sbt_num < e.sbt_den, needs: [] },
  { id: "vfd28", source: "episodes", agg: mean("vfd28"), denLabel: "episodes", missed: (e) => e.vfd28 === 0, needs: [] },
  { id: "imv_duration", source: "episodes", agg: median("imv_days"), denLabel: "episodes", missed: (e) => e.imv_days > 7, needs: [] },
  { id: "reintubation", source: "episodes", agg: ratio("reint_num", "ext_den"), denLabel: "extubations", missed: (e) => e.reint_num > 0, needs: [] },
  { id: "light_sedation", source: "episodes", agg: ratio("rass_num", "rass_den"), denLabel: "RASS readings", missed: (e) => e.rass_den > 0 && e.rass_num / e.rass_den < 0.5, needs: ["has_rass"] },
  { id: "deep_sedation", source: "episodes", agg: ratio("deep_num", "deep_den"), denLabel: "episodes", missed: (e) => e.deep_num > 0, needs: ["has_rass"] },
  { id: "paralytic_8h", source: "episodes", agg: ratio("para8_num", "para8_den"), denLabel: "intubated ICU days", missed: (e) => e.para8_num > 0, needs: [] },
  { id: "mortality", source: "episodes", agg: ratio("died", "outcome_den"), denLabel: "encounters", oe: "mort_expected", missed: (e) => e.died > 0, needs: [] },
  { id: "icu_los", source: "episodes", agg: median("icu_los"), denLabel: "encounters", missed: (e) => e.icu_los > 7, needs: [] },
  { id: "trach", source: "episodes", agg: ratio("trach_any", "outcome_den"), denLabel: "encounters", missed: (e) => e.trach_any > 0, needs: [] },
  { id: "icu_readmit", source: "episodes", agg: ratio("readmit_num", "readmit_den"), denLabel: "ICU transfers out", missed: (e) => e.readmit_num > 0, needs: [] },
];

export const DOMAINS = ["Lung protection", "Proning", "Liberation", "Sedation", "Outcomes"];

export function metricMeta(D, id) {
  return D.metrics[id];
}

const quant = (arr, q) => {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  const pos = (s.length - 1) * q;
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  return s[lo] + (s[hi] - s[lo]) * (pos - lo);
};

/** Aggregate rows for one metric → { value, num, den, n, q1, q3, observed, expected } */
export function aggregate(m, rows) {
  const a = m.agg;
  if (a.kind === "ratio") {
    let num = 0, den = 0, exp = 0, expN = 0;
    for (const r of rows) {
      const d = a.den ? r[a.den] || 0 : 1;
      if (!d) continue;
      num += r[a.num] || 0;
      den += d;
      if (m.oe && r[m.oe] != null) { exp += r[m.oe]; expN += 1; }
    }
    const out = { num, den, n: den, value: den ? (100 * num) / den : null };
    if (m.oe && expN) { out.expected = (100 * exp) / expN; out.oe = exp ? num / exp : null; out.expectedCount = exp; }
    return out;
  }
  const vals = rows.map((r) => r[a.field]).filter((v) => v != null && !Number.isNaN(v));
  if (a.kind === "mean") {
    const n = vals.length;
    const value = n ? vals.reduce((s, v) => s + v, 0) / n : null;
    const sd = n > 1 ? Math.sqrt(vals.reduce((s, v) => s + (v - value) ** 2, 0) / (n - 1)) : null;
    return { value, n, sd, q1: quant(vals, 0.25), q3: quant(vals, 0.75) };
  }
  return { value: quant(vals, 0.5), n: vals.length, q1: quant(vals, 0.25), q3: quant(vals, 0.75) };
}

export function isPercent(meta) {
  return meta.unit === "%";
}

export function fmt(v, meta, digits) {
  if (v == null || Number.isNaN(v)) return "—";
  if (meta.unit === "%") return `${v.toFixed(digits ?? (v < 10 ? 1 : 0))}%`;
  const d = digits ?? (Math.abs(v) < 10 ? 1 : 0);
  return `${v.toFixed(d)} ${meta.unit}`;
}

/** Target status: good | warning | critical | none */
export function status(value, meta) {
  if (value == null || meta.target == null || meta.direction === "neutral") return "none";
  const t = meta.target;
  const tol = meta.unit === "%" ? 5 : t * 0.1;
  if (meta.direction === "higher") return (meta.target_op === ">" ? value > t : value >= t) ? "good" : value >= t - tol ? "warning" : "critical";
  const ok = meta.target_op === "<" ? value < t : value <= t;
  return ok ? "good" : value <= t + tol ? "warning" : "critical";
}

export const STATUS_TEXT = { good: "Meets target", warning: "Near target", critical: "Below target", none: "No target set" };
export const STATUS_TEXT_LOWER = { good: "Meets target", warning: "Near target", critical: "Above target", none: "No target set" };
export const statusLabel = (s, meta) => (meta.direction === "lower" ? STATUS_TEXT_LOWER : STATUS_TEXT)[s];

/** p-chart control limits for a proportion series */
export function pChart(points) {
  const num = points.reduce((s, p) => s + (p.num || 0), 0);
  const den = points.reduce((s, p) => s + (p.den || 0), 0);
  if (!den) return null;
  const pbar = num / den;
  return {
    center: 100 * pbar,
    limits: points.map((p) => {
      if (!p.den) return null;
      const se = Math.sqrt((pbar * (1 - pbar)) / p.den);
      return { lo: Math.max(0, 100 * (pbar - 3 * se)), hi: Math.min(100, 100 * (pbar + 3 * se)) };
    }),
  };
}

export const STRATA = [
  { id: "unit", label: "ICU" },
  { id: "hospital", label: "Hospital" },
  { id: "sex", label: "Sex" },
  { id: "race_eth", label: "Race / ethnicity" },
  { id: "age_band", label: "Age band" },
  { id: "code_status", label: "Code status at IMV start" },
];

export const COMPLETENESS = [
  ["has_height", "Height recorded (for PBW)"],
  ["has_weight", "Weight recorded"],
  ["has_vt", "Set tidal volume in a volume mode"],
  ["has_plateau", "Plateau pressure recorded"],
  ["has_rass", "RASS during IMV"],
  ["has_abg", "Arterial blood gas"],
];
