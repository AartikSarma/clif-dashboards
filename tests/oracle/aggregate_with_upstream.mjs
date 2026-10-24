// Aggregate the oracle rows with upstream's own metrics.js, for tests/test_server.py.
//   node aggregate_with_upstream.mjs <oracle.json>   -> JSON on stdout
import { readFileSync } from "node:fs";
import { METRICS, aggregate, pChart } from "./upstream_metrics.js";

const data = JSON.parse(readFileSync(process.argv[2], "utf8"));
const rowsFor = (m) => (m.source === "proning" ? data.proning : data.episodes);
const months = [...new Set(data.episodes.map((r) => r.month).concat(data.proning.map((r) => r.month)))].sort();
const units = [...new Set(data.episodes.map((r) => r.unit).filter((v) => v != null))].sort();
const hospitals = [...new Set(data.episodes.map((r) => r.hospital).filter((v) => v != null))].sort();
const out = {};
for (const m of METRICS) {
  const rows = rowsFor(m);
  const by = {};
  for (const r of rows) (by[r.month] = by[r.month] || []).push(r);
  const series = months.map((mo) => ({ month: mo, ...aggregate(m, by[mo] || []) }));
  out[m.id] = {
    agg: aggregate(m, rows),
    series,
    control: m.agg.kind === "ratio" ? pChart(series) : null,
    units: Object.fromEntries(units.map((u) => [u, aggregate(m, rows.filter((r) => r.unit === u))])),
    hospitals: Object.fromEntries(hospitals.map((h) => [h, aggregate(m, rows.filter((r) => r.hospital === h))])),
  };
}
process.stdout.write(JSON.stringify(out));
