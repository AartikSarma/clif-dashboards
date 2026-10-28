"""What a dashboard module gives the platform, and what the platform gives it.

The platform (build.py, stage.py, server/) owns site configuration, encounter
stitching, chunked staging, filtering, aggregation and the API. A dashboard
module (src/clif_dash/dashboards/<id>/) exports one Dashboard object that says
which CLIF tables it reads, how it builds its rows, and which measures the
server aggregates from them. The platform calls the module only through it.
"""
from __future__ import annotations

import multiprocessing
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import stage
from .config import SiteConfig

try:
    import resource
except ImportError:  # Windows has no resource module
    resource = None

# Which rows missed a measure: (rows, the measure as resolved, variant included) -> boolean Series
MissedRule = Callable[[pd.DataFrame, "Metric"], pd.Series]


def peak_memory_gb() -> float | None:
    """This process's peak memory, or None where the OS does not report it (Windows).

    ru_maxrss is in bytes on macOS and in kilobytes on Linux.
    """
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (peak if sys.platform == "darwin" else peak * 1024) / 1e9


def log(message: str) -> None:
    peak = peak_memory_gb()
    memory = f" | peak {peak:.1f} GB" if peak is not None else ""
    print(f"[{time.strftime('%H:%M:%S')}{memory}] {message}", flush=True)


CHUNKS_PER_WORKER = 4


def assign_chunks(encounter_blocks, encounters_per_chunk: int, workers: int = 1) -> pd.DataFrame:
    """Split encounters into chunks of at most `encounters_per_chunk`.

    With several workers, chunks are also small enough that each worker gets about
    CHUNKS_PER_WORKER of them: one chunk per worker leaves the others idle while the
    slowest finishes, and a pool with fewer chunks than workers leaves workers unused.
    """
    blocks = np.sort(np.asarray(pd.unique(encounter_blocks)))
    size = encounters_per_chunk
    if workers > 1 and len(blocks):
        size = max(1, min(size, -(-len(blocks) // (workers * CHUNKS_PER_WORKER))))
    return pd.DataFrame({"encounter_block": blocks, "chunk": np.arange(len(blocks)) // size})


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

    def stage(self, table: str, chunk_map: pd.DataFrame, stage_name: str, where: str | None = None) -> int:
        """Stage `table` rows for the encounters in `chunk_map` under stage/<stage_name>/<table>.

        `where` optionally narrows the rows, as SQL on `source.<column>`.
        """
        return stage.stage_table(self.con, self.cfg, table, chunk_map, self.cfg.stage_dir / stage_name / table,
                                 where=where)

    def run_chunks(self, chunk_function: Callable, chunks) -> list:
        """Call chunk_function(cfg, chunk, site_statistics) for every chunk, in worker processes.

        Workers are spawned, so chunk_function must be a module-level function.
        Any statistic over a whole table belongs in site_statistics, never in a
        chunk: a chunk sees only its own encounters.

        Spawned on every platform, not forked: Linux forks by default, and a forked
        worker inherits the parent's polars, Arrow and DuckDB thread pools without
        their threads, so its first parallel call (clifpy's Charlson index, in
        polars) waits forever.
        """
        cfg, statistics = self.cfg, self.site_statistics
        if cfg.workers <= 1:
            return [chunk_function(cfg, c, statistics) for c in chunks]
        with ProcessPoolExecutor(max_workers=cfg.workers, mp_context=multiprocessing.get_context("spawn")) as pool:
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
    missed_label: str = "missed"     # how the list names rows the missed rule picks: "missed", "with any"
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
                "scale": self.scale if self.kind == "rate" else None, "variants": self.variants is not None,
                "missedLabel": self.missed_label}


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
class Heatmap:
    """A pattern over the day: one value per hour (rows) and month (columns), in site time.

    Events (`time_column`): each row of `source` with a time there is one event, placed by
    the hour and month of that time; with `where`, only rows whose `where` column is true or
    non-zero count. Cells are counts, or each month's share.

    Values (`hour_column`): rows already binned by local hour of day (that column) and
    `month`; each cell is scale × Σ numerator ÷ Σ denominator, a mean rate or a share of
    time, shown in `unit` (or the unit the build records under meta["heatmap_units"][id]).
    """
    id: str
    title: str
    source: str                  # which row table (RowTable.name)
    event: str                   # what is counted, plural: "ICU admissions"; for values, what the denominator sums
    time_column: str | None = None
    where: str | None = None
    hour_column: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    scale: float = 1.0
    unit: str = ""
    note: str = ""

    @property
    def kind(self) -> str:
        return "values" if self.hour_column else "events"

    def public(self, units: dict | None = None) -> dict:
        return {"id": self.id, "title": self.title, "event": self.event, "note": self.note, "kind": self.kind,
                "unit": (units or {}).get(self.id, self.unit)}


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
    heatmaps: tuple[Heatmap, ...] = ()                   # time-of-day patterns, on the Time of day page

    @property
    def metrics_by_id(self) -> dict[str, Metric]:
        return {m.id: m for m in self.metrics}

    def missing_required_tables(self, cfg: SiteConfig) -> list[Path]:
        return [cfg.table_path(t) for t in self.required_tables if not cfg.table_path(t).exists()]
