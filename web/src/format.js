// Display helpers. Aggregation runs on the server (src/clif_dash/server/aggregate.py);
// these only format values and compare them with the targets in decisions.yaml.

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

const STATUS_TEXT = { good: "Meets target", warning: "Near target", critical: "Below target", none: "No target set" };
const STATUS_TEXT_LOWER = { good: "Meets target", warning: "Near target", critical: "Above target", none: "No target set" };
export const statusLabel = (s, meta) => (meta.direction === "lower" ? STATUS_TEXT_LOWER : STATUS_TEXT)[s];
