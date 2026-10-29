"""Aggregate export: a built dashboard's results without its patient rows, for a shared server.

`clif-dash export` reads each built dashboard (output/<site>/<dashboard>/, which holds
PHI) and writes a folder that holds none, to copy to a server that runs
`clif-dash serve --aggregates <folder>`:

- each row table as a cube (src/clif_dash/cube.py): sums over every combination of
  the export's dimensions, with no IDs, dates or individual values;
- each counted-event heatmap as counts by the filters, the event's month, day of
  week and hour; binned tables (liberation's sedation_hours) as they are;
- summary.json without the site's data folder and its first and last record
  times, which are single patients' timestamps.

The staged chunks, encounter mapping and timelines are not copied.

The dimensions decide how fine the cells are. By default they are every filter and
breakdown a table has (month, hospital, ICU, sex, race and ethnicity, age band and the
dashboard's own filters), so the shared server offers every page the site's own does
except the patient lists and timelines. Crossing them all leaves many cells with a
single patient; the dashboards are for the site's own reviewers, who may see that.
For a wider audience, COARSE_DIMENSIONS (month, hospital and ICU) keeps cells large
and drops the other filters. The export reports, per table, how many cells hold one row.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

from .config import SiteConfig
from .cube import CELL_KEYS, N_ROWS, value_bin
from .dashboard import Dashboard

FORMAT = 1
ALL = "all"     # every filter and breakdown column a table has
DEFAULT_DIMENSIONS = ALL
COARSE_DIMENSIONS = ("month", "hospital", "unit")


def cell_keys(dashboard: Dashboard, rows: pd.DataFrame, dimensions=DEFAULT_DIMENSIONS) -> list[str]:
    """The columns that place a row in a cell: the export's dimensions that the table has. With ALL,
    the shared filters, the dashboard's own filters and its breakdowns."""
    if dimensions == ALL:
        dimensions = [*CELL_KEYS, *(f.column for f in dashboard.filters), *(column for column, _ in dashboard.strata)]
    return [k for k in dict.fromkeys(dimensions) if k in rows.columns]


def summed_columns(dashboard: Dashboard, table: str, rows: pd.DataFrame, meta: dict) -> list[str]:
    """Every column a measure on `table` adds up, with its variants, plus the completeness flags."""
    columns = []
    for metric in dashboard.metrics:
        if metric.source != table:
            continue
        if metric.kind in ("ratio", "rate"):
            columns += [metric.numerator, metric.denominator, *metric.extra, *(c for c, _ in metric.parts),
                        *metric.needs]
            if metric.variants is not None:
                columns += [metric.variants.column(v) for v in meta.get(metric.variants.built_values, [])]
    columns += [key for key, _ in dashboard.completeness]
    columns += ["has_height"]
    return [c for c in dict.fromkeys(columns) if c and c in rows.columns]


def cube(dashboard: Dashboard, table: str, rows: pd.DataFrame, meta: dict, dimensions=DEFAULT_DIMENSIONS) -> pd.DataFrame:
    """One table's rows summed into cells."""
    keys = cell_keys(dashboard, rows, dimensions)
    work = rows[keys].copy()
    work[N_ROWS] = 1
    sums = summed_columns(dashboard, table, rows, meta)
    for column in sums:
        work[column] = pd.to_numeric(rows[column], errors="coerce").astype(float)
    for metric in dashboard.metrics:
        if metric.source != table:
            continue
        if metric.kind == "rate":
            # How many rows contribute exposure: a rate's n
            work[f"{metric.denominator}__nz"] = (pd.to_numeric(rows[metric.denominator]).fillna(0) > 0).astype(int)
        if metric.kind == "ratio" and metric.expected:
            # Expected deaths or pronings over the rows the measure counts, for observed/expected
            denominator = rows[metric.denominator].fillna(0) if metric.denominator else pd.Series(1, index=rows.index)
            expected = rows[metric.expected].where(denominator != 0)
            work[f"{metric.id}__expected_sum"] = expected.fillna(0)
            work[f"{metric.id}__expected_n"] = expected.notna().astype(int)
        if metric.kind in ("mean", "median"):
            values = pd.to_numeric(rows[metric.numerator], errors="coerce").astype(float)
            work[f"{metric.numerator}__n"] = values.notna().astype(int)
            work[f"{metric.numerator}__sum"] = values.fillna(0)
            work[f"{metric.numerator}__sumsq"] = (values ** 2).fillna(0)
    added = [c for c in work.columns if c not in keys]
    cells = work.groupby(keys, dropna=False, observed=True)[added].sum().reset_index()
    for column in dict.fromkeys(m.numerator for m in dashboard.metrics
                                if m.source == table and m.kind in ("mean", "median")):
        values = pd.to_numeric(rows[column], errors="coerce").astype(float)
        present = values.notna()
        binned = rows.loc[present, keys].assign(bin=value_bin(values[present]).to_numpy())
        counts = binned.groupby([*keys, "bin"], dropna=False, observed=True).size().rename("count").reset_index()
        lists = counts.groupby(keys, dropna=False, observed=True).agg(
            **{f"{column}__bins": ("bin", list), f"{column}__counts": ("count", list)}).reset_index()
        cells = cells.merge(lists, on=keys, how="left")
    return cells


def event_counts(dashboard: Dashboard, rows: pd.DataFrame, time_column: str, where: str | None, tz: str,
                 dimensions=DEFAULT_DIMENSIONS) -> pd.DataFrame:
    """A counted-event heatmap's events by the export's dimensions, the event's local month, day of week and hour."""
    keys = cell_keys(dashboard, rows, dimensions)
    if where is not None:
        rows = rows[rows[where].fillna(0).astype(bool)]
    rows = rows[rows[time_column].notna()]
    local = rows[time_column].dt.tz_convert(tz)
    events = rows[keys].assign(event_month=local.dt.strftime("%Y-%m").to_numpy(),
                               weekday=local.dt.dayofweek.to_numpy(), hour=local.dt.hour.to_numpy())
    return events.groupby([*keys, "event_month", "weekday", "hour"], dropna=False, observed=True).size() \
        .rename("count").reset_index()


def scrubbed_summary(summary: dict, cells: dict) -> dict:
    """summary.json without the data folder and the first and last record times (single patients' timestamps)."""
    meta = {k: v for k, v in summary["meta"].items() if not k.endswith("_records") and k != "data_folder"}
    return {**summary, "meta": meta, "aggregate": {"format": FORMAT, "cells": cells}}


def cell_report(table: pd.DataFrame) -> dict:
    """How many cells a cube has, the rows in them, and how many cells hold a single row."""
    return {"cells": int(len(table)), "rows": int(table[N_ROWS].sum()), "single_row_cells": int((table[N_ROWS] == 1).sum())}


def export_dashboard(cfg: SiteConfig, dashboard: Dashboard, destination: Path, dimensions=DEFAULT_DIMENSIONS) -> dict:
    """Write one built dashboard's aggregates to destination/<dashboard id>; returns its cell report by table."""
    source = cfg.dashboard_dir
    summary = json.loads((source / "summary.json").read_text())
    target = destination / dashboard.id
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    tables = {t.name: pd.read_parquet(source / f"{t.name}.parquet") for t in dashboard.row_tables}
    cells = {}
    for table in dashboard.row_tables:
        rows = tables[table.name]
        if table.binned:
            written = rows
        else:
            written = cube(dashboard, table.name, rows, summary["meta"], dimensions)
            cells[table.name] = cell_report(written)
        written.to_parquet(target / f"{table.name}.parquet", index=False)
    for heatmap in dashboard.heatmaps:
        if heatmap.kind == "events":
            event_counts(dashboard, tables[heatmap.source], heatmap.time_column, heatmap.where, cfg.site_timezone,
                         dimensions).to_parquet(target / f"heatmap__{heatmap.id}.parquet", index=False)
    (target / "summary.json").write_text(json.dumps(scrubbed_summary(summary, cells), indent=1, default=str))
    return cells


def export(cfg: SiteConfig, dashboards: list[Dashboard], destination: Path,
           dimensions=DEFAULT_DIMENSIONS) -> dict[str, dict]:
    """Export every built dashboard among `dashboards`; returns each one's cell report by table."""
    destination.mkdir(parents=True, exist_ok=True)
    reports = {}
    for dashboard in dashboards:
        bound = cfg.bind(dashboard.id)
        if (bound.dashboard_dir / "summary.json").exists():
            reports[dashboard.id] = export_dashboard(bound, dashboard, destination, dimensions)
    (destination / "export.json").write_text(json.dumps({
        "format": FORMAT, "site_name": cfg.site_name, "site_timezone": cfg.site_timezone,
        "dashboards": list(reports), "dimensions": dimensions if dimensions == ALL else list(dimensions),
    }, indent=1))
    return reports


def aggregate_config(folder: Path) -> SiteConfig:
    """A config for serving an export folder: its dashboards, site name and time zone, and nothing to read."""
    manifest = json.loads((folder / "export.json").read_text())
    return SiteConfig(clif_dir=folder, file_prefix="", site_name=manifest["site_name"],
                      site_timezone=manifest["site_timezone"], start_date=None, end_date=None, decisions_dir=folder,
                      decision_files={}, output_dir=folder, encounters_per_chunk=0, cohort_encounters_per_chunk=0,
                      workers=1, dashboards=tuple(manifest["dashboards"]))
