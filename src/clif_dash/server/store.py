"""Built outputs held in memory by the server, with the filters every page shares.

A dashboard's row tables are small (one row per IMV episode, say), so the
server keeps them as DataFrames. Detail views such as the respiratory timeline
are not precomputed; the dashboard builds one from its staged data on request.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import SiteConfig
from ..dashboard import FilterField, RowTable

# Filters every dashboard offers when its primary rows carry the column; each dashboard adds its own
SHARED_FILTERS = (FilterField("hospital", "Hospital"), FilterField("unit", "ICU"), FilterField("age_band", "Age band"),
                  FilterField("sex", "Sex"), FilterField("race_eth", "Race / ethnicity"))


def jsonable(value):
    """Upstream's JSON formatting (build_dashboard._clean): floats to 4 places, NaN to null."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if (math.isnan(value) or math.isinf(value)) else round(float(value), 4)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if value is pd.NaT or value is None:
        return None
    if isinstance(value, (pd.Timestamp, dt.date)):
        return str(value)
    return value


@dataclass
class Filters:
    month_from: str | None = None
    month_to: str | None = None
    values: dict[str, list[str]] = field(default_factory=dict)   # column -> allowed values
    flags: dict[str, bool] = field(default_factory=dict)         # column -> required value

    def apply(self, rows: pd.DataFrame) -> pd.DataFrame:
        keep = pd.Series(True, index=rows.index)
        if self.month_from:
            keep &= rows["month"] >= self.month_from
        if self.month_to:
            keep &= rows["month"] <= self.month_to
        for column, allowed in self.values.items():
            if allowed and column in rows.columns:
                keep &= rows[column].isin(allowed)
        # A table without the column is left alone: respiratory proning rows carry no
        # tracheostomy flag, and upstream did not filter them by it
        for column, required in self.flags.items():
            if column in rows.columns:
                keep &= rows[column] == required
        return rows[keep]


def display_rows(rows: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Rows as the list, CSV download and detail header show them: local times, no internal keys."""
    display = rows.drop(columns=["encounter_block"], errors="ignore").copy()
    for column in display.columns:
        if isinstance(display[column].dtype, pd.DatetimeTZDtype):
            display[column] = display[column].dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M")
    return display


class Store:
    """One dashboard's built row tables and summary. `cfg` is bound to the dashboard."""

    def __init__(self, cfg: SiteConfig, row_tables: tuple[RowTable, ...], filters: tuple[FilterField, ...] = ()):
        self.cfg = cfg
        output = cfg.dashboard_dir
        self.summary = json.loads((output / "summary.json").read_text())
        self.decisions = self.summary["decisions"]
        self.meta = self.summary["meta"]
        self.row_tables = row_tables
        self.dashboard_filters = filters
        self.primary = row_tables[0]
        self.tables: dict[str, pd.DataFrame] = {}
        self.displays: dict[str, pd.DataFrame] = {}
        for table in row_tables:
            rows = pd.read_parquet(output / f"{table.name}.parquet")
            if rows.empty:
                rows = pd.DataFrame(columns=[table.id_column, "month"])
            self.tables[table.name] = rows
            self.displays[table.name] = display_rows(rows, cfg.site_timezone).set_index(table.id_column, drop=False)
        primary_display = self.displays[self.primary.name]
        records = jsonable(primary_display.to_dict("records"))
        self.search_text = pd.Series(
            [" ".join("" if v is None else str(v) for v in r.values()).lower() for r in records],
            index=primary_display[self.primary.id_column].to_numpy())

    @property
    def primary_rows(self) -> pd.DataFrame:
        return self.tables[self.primary.name]

    def filter_fields(self) -> list[FilterField]:
        """The shared filters this dashboard's primary rows carry, then the dashboard's own."""
        return [f for f in (*SHARED_FILTERS, *self.dashboard_filters) if f.column in self.primary_rows.columns]

    def filter_options(self) -> dict[str, list[str]]:
        return {f.column: sorted(self.primary_rows[f.column].dropna().astype(str).unique().tolist())
                for f in self.filter_fields() if f.kind == "values"}
