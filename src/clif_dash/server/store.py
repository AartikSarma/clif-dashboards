"""Built outputs held in memory by the server, with the filters every page shares.

The episode and proning tables are small (one row per IMV episode or proning
patient), so the server keeps them as DataFrames. Timelines are not
precomputed: the respiratory dashboard's timeline.py builds one from the staged chunk when it is asked for.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import SiteConfig

FILTER_FIELDS = [("hospital", "Hospital"), ("unit", "ICU"), ("age_band", "Age band"), ("sex", "Sex"),
                 ("race_eth", "Race / ethnicity"), ("code_status", "Code status at IMV start")]


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
    values: dict[str, list[str]] = field(default_factory=dict)
    trach: str | None = None   # "yes", "no" or None

    def apply(self, rows: pd.DataFrame) -> pd.DataFrame:
        keep = pd.Series(True, index=rows.index)
        if self.month_from:
            keep &= rows["month"] >= self.month_from
        if self.month_to:
            keep &= rows["month"] <= self.month_to
        for column, allowed in self.values.items():
            if allowed and column in rows.columns:
                keep &= rows[column].isin(allowed)
        # Proning rows carry no tracheostomy flag, so this filter leaves them alone (as upstream did)
        if self.trach in ("yes", "no") and "trach" in rows.columns:
            keep &= rows["trach"] == (self.trach == "yes")
        return rows[keep]


class Store:
    def __init__(self, cfg: SiteConfig):
        self.cfg = cfg
        output = cfg.dashboard_dir
        self.summary = json.loads((output / "summary.json").read_text())
        self.decisions = self.summary["decisions"]
        self.meta = self.summary["meta"]
        self.episodes = pd.read_parquet(output / "episodes.parquet")
        self.proning = pd.read_parquet(output / "proning.parquet")
        if self.proning.empty:
            self.proning = pd.DataFrame(columns=["episode_id", "month"])
        tz = cfg.site_timezone
        # Display rows: what the episode list, CSV download and timeline header show
        display = self.episodes.drop(columns=["encounter_block"]).copy()
        for column in ("start", "end"):
            display[column] = self.episodes[column].dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M")
        self.episode_display = display.set_index("episode_id", drop=False)
        records = jsonable(display.to_dict("records"))
        self.search_text = pd.Series(
            [" ".join("" if v is None else str(v) for v in r.values()).lower() for r in records],
            index=display["episode_id"].to_numpy())
        proning_display = self.proning.copy()
        for column in ("t_enroll", "t_first_abg"):
            if column in proning_display.columns:
                proning_display[column] = proning_display[column].dt.tz_convert(tz).dt.strftime("%Y-%m-%d %H:%M")
        self.proning_display = proning_display.drop(columns=["encounter_block"], errors="ignore")
        self.small_cell_n = next((d["value"] for d in self.decisions["shared"] if d["id"] == "small_cell_n"), 11)

    def filter_options(self) -> dict[str, list[str]]:
        return {column: sorted(self.episodes[column].dropna().astype(str).unique().tolist())
                for column, _ in FILTER_FIELDS}
