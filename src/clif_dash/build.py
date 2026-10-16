"""Batch build: CLIF tables -> one dashboard's row tables and summary.

The platform's part of every build:

1. Read patient, hospitalization and adt whole and stitch encounters across the
   site (upstream's prep.build_encounters with clifpy's stitcher).
2. Compute the dashboard's statistics over whole source tables (its
   site_statistics hook), once, before any chunk runs.
3. Hand the site to the dashboard's build hook, which stages its own tables in
   chunks and returns its row tables (src/clif_dash/dashboards/<id>/build.py).
4. Count rows in every source table the dashboard reads, add the dashboard's own
   data-quality counts, and write the row tables and summary.json.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import stage
from .config import SiteConfig
from .dashboard import Dashboard, SiteBuild, log
from .dashboards import DEFAULT_DASHBOARD
from .upstream import clif_io, prep

PIPELINE_VERSION = "0.1.0"
UPSTREAM_COMMIT = "b3cdf29"


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


def build_encounters(cfg: SiteConfig, decisions: dict):
    shared = shared_values(decisions)
    small_tables = {t: stage.read_whole_table(cfg, t) for t in stage.ENCOUNTER_TABLES}
    _, stitch_fn, clifpy_source = clif_io.clifpy_functions()
    encounters, mapping, adt = prep.build_encounters(
        small_tables, stitch_fn, shared["stitch_hours"], shared["age_min"], log=lambda m: log(m.strip()))
    return encounters, mapping, adt, clifpy_source


def data_quality(con, cfg: SiteConfig, dashboard: Dashboard) -> dict:
    """Counts over whole source tables that every dashboard reports."""
    def scalar(sql):
        return int(con.execute(sql).fetchone()[0])

    tables = [*stage.ENCOUNTER_TABLES, *dashboard.tables]
    return {
        "row_counts": {t: scalar(f"SELECT count(*) FROM read_parquet('{cfg.table_path(t)}')")
                       for t in tables if cfg.table_path(t).exists()},
        "icu_rows_without_location_type": scalar(
            f"SELECT count(*) FROM read_parquet('{cfg.table_path('adt')}') "
            "WHERE lower(location_category) = 'icu' AND location_type IS NULL"),
    }


def build(cfg: SiteConfig, dashboard: Dashboard = DEFAULT_DASHBOARD) -> Path:
    missing = dashboard.missing_required_tables(cfg)
    if missing:
        raise FileNotFoundError(f"Required CLIF tables not found: {', '.join(map(str, missing))}")
    decisions = cfg.decisions()
    work = cfg.stage_dir
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    con = stage.connect()
    started = time.time()

    log(f"Encounters from {cfg.clif_dir}")
    encounters, mapping, adt, clifpy_source = build_encounters(cfg, decisions)
    encounters.to_parquet(work / "encounters.parquet", index=False)
    mapping.to_parquet(work / "mapping.parquet", index=False)
    adt.to_parquet(work / "adt.parquet", index=False)
    statistics = dashboard.site_statistics(con, cfg)

    result = dashboard.build(SiteBuild(cfg=cfg, con=con, encounters=encounters, mapping=mapping, adt=adt,
                                       site_statistics=statistics))

    quality = data_quality(con, cfg, dashboard)
    quality.update(dashboard.data_quality(con, cfg))
    quality.update(result.data_quality)

    output = cfg.output_dir
    for name, rows in result.tables.items():
        rows.to_parquet(output / f"{name}.parquet", index=False)
    summary = {
        "meta": {
            "site_name": cfg.site_name,
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_folder": cfg.clif_dir.name,
            "timezone": cfg.site_timezone,
            "clifpy": clifpy_source,
            "pipeline_version": PIPELINE_VERSION,
            "upstream_commit": UPSTREAM_COMMIT,
            **result.meta,
            **statistics,
        },
        "decisions": decisions,
        **result.summary,
        "data_quality": quality,
    }
    (output / "summary.json").write_text(json.dumps(summary, default=_json_default, ensure_ascii=False, indent=1))
    log(f"Wrote {output} ({dashboard.id}) in {time.time() - started:.0f} s")
    return output


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, dt.date)):
        return str(value)
    raise TypeError(f"Not JSON serializable: {type(value)}")


if __name__ == "__main__":
    sys.exit("Run through the CLI: uv run clif-dash build --config <site.yaml>")
