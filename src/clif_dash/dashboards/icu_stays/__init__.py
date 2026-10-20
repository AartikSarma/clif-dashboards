"""ICU stays: a minimal second dashboard that tests the platform, not a quality report.

It exists to prove that two dashboards build and serve side by side, and that
the server does not assume respiratory columns. It reads only the stitched
encounters and ADT, so it needs no staging and no worker processes. Remove it
once a real second dashboard (stewardship, sepsis) takes over that role. It runs
only when a site config names it under `dashboards:`.
"""
from __future__ import annotations

import pandas as pd

from ...config import SiteConfig
from ...dashboard import BuildResult, Dashboard, Metric, RowTable, SiteBuild
from ...upstream import prep

STAYS = RowTable("stays", "stay_id", "ICU stay", "ICU stays")
METRICS = [
    Metric("hospital_mortality", "stays", "ratio", "died", None, "ICU stays",
           missed=lambda rows: rows["died"].fillna(0) > 0),
    Metric("icu_days", "stays", "median", "icu_days", den_label="ICU stays",
           missed=lambda rows: rows["icu_days"] > 7),
    Metric("deaths_per_icu_days", "stays", "rate", "died", "icu_days", "ICU days", scale=1000,
           missed=lambda rows: rows["died"].fillna(0) > 0),
]


def build(site: SiteBuild) -> BuildResult:
    """One row per adult encounter with any ICU time, attributed to its first ICU stay."""
    cfg = site.cfg
    adults = site.encounters[site.encounters["adult"]].set_index("encounter_block")
    icu = site.adt[site.adt["location_category"].str.lower().eq("icu")
                   & site.adt["encounter_block"].isin(adults.index)]
    icu = icu.assign(hours=(icu["out_dttm"] - icu["in_dttm"]).dt.total_seconds() / 3600)
    icu = icu.sort_values(["encounter_block", "in_dttm"], kind="stable")
    stays = icu.groupby("encounter_block").agg(
        start=("in_dttm", "min"), end=("out_dttm", "max"), icu_hours=("hours", "sum"),
        location_type=("location_type", "first"), hospital=("hospital_id", "first"))
    encounters = adults.loc[stays.index]
    stays["stay_id"] = encounters["hospitalization_id"]
    stays["unit"] = stays["location_type"].map(prep.unit_label)
    stays["hospital"] = stays["hospital"].astype(str)
    stays["month"] = stays["start"].dt.tz_convert(cfg.site_timezone).dt.strftime("%Y-%m")
    stays["sex"] = encounters["sex_category"].fillna("Unknown")
    stays["race_eth"] = [prep.race_eth(r, e) for r, e in zip(encounters["race_category"], encounters["ethnicity_category"])]
    stays["age_band"] = encounters["age"].map(prep.age_band)
    stays["died"] = encounters["discharge_category"].astype(str).str.lower().eq("expired").astype(int)
    stays["icu_days"] = stays["icu_hours"] / 24
    rows = stays.reset_index()[["stay_id", "encounter_block", "start", "end", "month", "hospital", "unit",
                                "sex", "race_eth", "age_band", "died", "icu_days"]]
    funnel = [
        {"step": "Encounters after linking", "n": int(len(site.encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with any ICU stay", "n": int(len(rows))},
    ]
    return BuildResult(tables={"stays": rows.sort_values("stay_id").reset_index(drop=True)},
                       summary={"funnel": funnel},
                       meta={"months": sorted(rows["month"].dropna().unique().tolist())})


def resolve(route_id: str, decisions: dict, ltvv_thresholds: list):
    by_id = {m.id: m for m in METRICS}
    return by_id.get(str(route_id), METRICS[0]), None, None


def no_statistics(con, cfg: SiteConfig) -> dict:
    return {}


ICU_STAYS = Dashboard(
    id="icu_stays",
    title="ICU stays (platform test)",
    tables=(),
    required_tables=(),
    site_statistics=no_statistics,
    build=build,
    data_quality=no_statistics,
    row_tables=(STAYS,),
    metrics=METRICS,
    domains=["ICU stays"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    independent_denominators=frozenset({"ICU stays", "ICU days"}),
    resolve=resolve,
)
