"""What a dashboard module gives the platform, and what the platform gives it.

The platform (build.py, stage.py, server/) owns site configuration, encounter
stitching, chunked staging, filtering, aggregation and the API. A dashboard
module (src/clif_dash/dashboards/<id>/) exports one Dashboard object that says
which CLIF tables it reads, how it builds its rows, and which measures the
server aggregates from them. The platform calls the module only through it.
"""
from __future__ import annotations

import resource
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import stage
from .config import SiteConfig

# Which rows missed a measure: (rows, the measure as resolved, variant included) -> boolean Series
MissedRule = Callable[[pd.DataFrame, "Metric"], pd.Series]


def log(message: str) -> None:
    peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9  # bytes on macOS
    print(f"[{time.strftime('%H:%M:%S')} | peak {peak_gb:.1f} GB] {message}", flush=True)


def assign_chunks(encounter_blocks, encounters_per_chunk: int) -> pd.DataFrame:
    blocks = np.sort(np.asarray(pd.unique(encounter_blocks)))
    return pd.DataFrame({"encounter_block": blocks, "chunk": np.arange(len(blocks)) // encounters_per_chunk})


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
@dataclass
class SiteBuild:
    """The site as the platform hands it to a dashboard's build.

    `encounters`, `mapping` and `adt` are stitched once for the whole site and
    also written to the stage folder. `site_statistics` holds whatever the
    dashboard's own site_statistics hook computed over whole source tables.
    """
    cfg: SiteConfig
    con: Any                      # DuckDB connection
    encounters: pd.DataFrame
    mapping: pd.DataFrame
    adt: pd.DataFrame
    site_statistics: dict

    def stage(self, table: str, chunk_map: pd.DataFrame, stage_name: str) -> int:
        """Stage `table` rows for the encounters in `chunk_map` under stage/<stage_name>/<table>."""
        return stage.stage_table(self.con, self.cfg, table, chunk_map, self.cfg.stage_dir / stage_name / table)

    def run_chunks(self, chunk_function: Callable, chunks) -> list:
        """Call chunk_function(cfg, chunk, site_statistics) for every chunk, in worker processes.

        Workers are spawned, so chunk_function must be a module-level function.
        Any statistic over a whole table belongs in site_statistics, never in a
        chunk: a chunk sees only its own encounters.
        """
        cfg, statistics = self.cfg, self.site_statistics
        if cfg.workers <= 1:
            return [chunk_function(cfg, c, statistics) for c in chunks]
        with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
            return list(pool.map(chunk_function, [cfg] * len(chunks), chunks, [statistics] * len(chunks)))


@dataclass
class BuildResult:
    """A dashboard's built rows, written to the output folder, and its part of summary.json."""
    tables: dict[str, pd.DataFrame]                       # name -> rows, written as <name>.parquet
    summary: dict = field(default_factory=dict)           # top-level summary keys (funnels)
    meta: dict = field(default_factory=dict)              # added to summary["meta"]
    data_quality: dict = field(default_factory=dict)      # added to summary["data_quality"]


# --------------------------------------------------------------------------- #
# Measures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Variants:
    """One measure computed at several values of a parameter, chosen in the route id ("ltvv@7").

    The build computes a numerator column for each value and lists the values in
    summary meta; the default is the value of one of the measure's decisions.
    """
    decision: str                    # id of the measure's decision that holds the default value
    built_values: str                # summary meta key listing the values the build computed
    column: Callable[[float], str]   # numerator column for a value


@dataclass(frozen=True)
class Metric:
    id: str
    source: str                      # which row table (RowTable.name)
    kind: str                        # "ratio", "rate", "mean" or "median"
    numerator: str | None = None     # ratio numerator or rate events column, or the mean/median column
    denominator: str | None = None   # ratio denominator or rate exposure column; for a ratio, None means one per row
    den_label: str = "episodes"
    expected: str | None = None      # column with each row's expected probability (O/E)
    extra: tuple[str, ...] = ()      # further one-per-row ratios shown beside the headline
    parts: tuple[tuple[str, str], ...] = ()  # (numerator column, label) sharing the denominator
    needs: tuple[str, ...] = ()      # completeness flags this measure depends on
    scale: float = 1000.0            # a rate's events are per this much exposure (per 1,000 patient-days)
    variants: Variants | None = field(default=None, compare=False)
    missed: MissedRule = field(default=lambda rows, metric: pd.Series(False, index=rows.index), compare=False)

    def missed_rows(self, rows: pd.DataFrame) -> pd.Series:
        return self.missed(rows, self)

    def share(self, rows: pd.DataFrame) -> pd.Series:
        """Each row's own numerator ÷ denominator (no value where the denominator is zero)."""
        return rows[self.numerator] / rows[self.denominator].where(rows[self.denominator] > 0)

    def public(self) -> dict:
        return {"id": self.id, "source": self.source, "kind": self.kind, "numerator": self.numerator,
                "denominator": self.denominator, "denLabel": self.den_label, "oe": self.expected,
                "extra": list(self.extra), "parts": [list(p) for p in self.parts], "needs": list(self.needs),
                "scale": self.scale if self.kind == "rate" else None, "variants": self.variants is not None}


def resolve_route(route_id: str, metrics: list[Metric], decisions: dict, meta: dict):
    """The measure a route id names, at the variant it asks for: "ltvv@7", or "plateau".

    Returns (metric, value, default). An unknown measure falls back to the first;
    a variant value the build did not compute falls back to the default. value and
    default are None for a measure without variants.
    """
    metric_id, _, value_text = str(route_id).partition("@")
    metric = next((m for m in metrics if m.id == metric_id), metrics[0])
    if metric.variants is None:
        return metric, None, None
    variants = metric.variants
    default = next(d["value"] for d in decisions["metrics"][metric.id]["decisions"] if d["id"] == variants.decision)
    built = meta.get(variants.built_values, [])
    try:
        value = float(value_text) if value_text else default
    except ValueError:
        value = default
    if value not in built:
        value = default
    numerator = variants.column(value) if value in built else metric.numerator
    return replace(metric, numerator=numerator), value, default


@dataclass(frozen=True)
class RowTable:
    """One table of built rows that measures aggregate, as <name>.parquet in the dashboard folder.

    The dashboard's first row table is its primary one: the rows its list page
    shows, its units are taken from, and its detail view opens. Any other table
    must carry the primary table's ID column, which links its rows back.
    """
    name: str                    # file name and Metric.source
    id_column: str
    unit: str                    # what one row is: "IMV episode"
    unit_plural: str             # "IMV episodes"

    def public(self) -> dict:
        return {"name": self.name, "id_column": self.id_column, "unit": self.unit, "unit_plural": self.unit_plural}


@dataclass(frozen=True)
class FilterField:
    """A filter in the left rail, applied to every row table that has the column."""
    column: str
    label: str
    kind: str = "values"         # "values": any of the column's values; "flag": yes or no on a boolean column

    def public(self) -> list:
        return [self.column, self.label, self.kind]


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Dashboard:
    id: str
    title: str
    # CLIF tables read beyond patient, hospitalization and adt (which every dashboard gets)
    tables: tuple[str, ...]
    required_tables: tuple[str, ...]
    # Build hooks
    site_statistics: Callable[[Any, SiteConfig], dict]   # (con, cfg): statistics over whole source tables
    build: Callable[[SiteBuild], BuildResult]
    data_quality: Callable[[Any, SiteConfig], dict]      # (con, cfg): counts over whole source tables
    # Server
    row_tables: tuple[RowTable, ...]                     # primary table first
    metrics: list[Metric]
    domains: list[str]
    strata: list[tuple[str, str]]
    filters: tuple[FilterField, ...]                     # added after the platform's shared filters
    completeness: list[tuple[str, str]]
    independent_denominators: frozenset[str]             # denominators that get p-chart limits
    detail: Callable | None = None                       # (store) -> builder with .build(row_id)

    @property
    def metrics_by_id(self) -> dict[str, Metric]:
        return {m.id: m for m in self.metrics}

    def missing_required_tables(self, cfg: SiteConfig) -> list[Path]:
        return [cfg.table_path(t) for t in self.required_tables if not cfg.table_path(t).exists()]
