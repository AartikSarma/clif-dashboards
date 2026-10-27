"""Glycemic control build: glucose by ICU day, with insulin infusions, by ICU and month.

Every choice is in config/decisions/glycemic.yaml. Days present come from ADT
(src/clif_dash/icu.py). DuckDB places each glucose result in the ICU stay that
contains its collection time and summarizes it per encounter, unit and local
day; insulin infusions become the ICU days on which one ran. Only those daily
summaries reach pandas. Rows are one encounter's days in one unit at one
hospital in one month.
"""
from __future__ import annotations

import pandas as pd

from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, log
from ...icu import (DAY_KEY, days_present, frame_days, icu_intervals, icu_session, in_window, infusion_runs_sql,
                    interval_days_sql, outlier_bounds, placed_sql, quoted, utc_text, whole_months)
from ...upstream import prep

TABLES = ("labs", "medication_admin_continuous")
REQUIRED_TABLES = ("labs",)
STAY = ["encounter_block", "hospital", "unit", "month"]
DAY_COUNTS = ["glucose_days", "days_severe_hypo", "days_hypo", "days_severe_hyper", "days_in_range",
              "insulin_days", "insulin_glucose_days", "insulin_hypo_days"]


TIME_COLUMNS = ("lab_collect_dttm", "lab_result_dttm")


def shared_values(decisions: dict) -> dict:
    shared = {d["id"]: d["value"] for d in decisions["shared"]}
    if shared["glucose_time"] not in TIME_COLUMNS:
        raise ValueError(f"glucose_time must be one of {TIME_COLUMNS}, not {shared['glucose_time']!r}")
    return shared


def threshold(decisions: dict, measure: str) -> float | list:
    return next(d["value"] for d in decisions["metrics"][measure]["decisions"] if d["id"] == "threshold")


def glucose_filter(categories: list[str], value: str = "s.lab_value_numeric", category: str = "s.lab_category") -> str:
    """SQL keeping glucose results within clifpy's plausible range for their category."""
    ranges = []
    for name in categories:
        low, high = outlier_bounds("labs", "lab_value_numeric", name.lower())
        ranges.append(f"(lower(CAST({category} AS VARCHAR)) = '{name.lower()}' AND {value} BETWEEN {low} AND {high})")
    return " OR ".join(ranges) or "FALSE"


def site_statistics(con, cfg: SiteConfig) -> dict:
    """When the site's glucose results begin and end, and the span of admissions."""
    shared = shared_values(cfg.decisions())
    first, last = con.execute(
        f"SELECT min({shared['glucose_time']}), max({shared['glucose_time']}) "
        f"FROM read_parquet('{cfg.table_path('labs')}') "
        f"WHERE lower(CAST(lab_category AS VARCHAR)) IN ({quoted(shared['glucose_categories'])})").fetchone()
    first_admission, last_admission = con.execute(
        f"SELECT min(admission_dttm), max(admission_dttm) FROM read_parquet('{cfg.table_path('hospitalization')}')"
    ).fetchone()
    return {"glucose_records": {"first": utc_text(first), "last": utc_text(last),
                                "first_admission": utc_text(first_admission), "last_admission": utc_text(last_admission)}}


def data_quality(con, cfg: SiteConfig) -> dict:
    shared = shared_values(cfg.decisions())
    source = f"read_parquet('{cfg.table_path('labs')}')"
    glucose = f"lower(CAST(lab_category AS VARCHAR)) IN ({quoted(shared['glucose_categories'])})"

    def scalar(sql):
        return int(con.execute(sql).fetchone()[0] or 0)

    return {
        "glucose_rows_by_category": {str(k): int(n) for k, n in con.execute(
            f"SELECT lower(CAST(lab_category AS VARCHAR)), count(*) FROM {source} WHERE {glucose} GROUP BY 1 "
            "ORDER BY 2 DESC").fetchall()},
        "glucose_rows_by_unit": {str(k): int(n) for k, n in con.execute(
            f"SELECT CAST(reference_unit AS VARCHAR), count(*) FROM {source} WHERE {glucose} GROUP BY 1 "
            "ORDER BY 2 DESC").fetchall()},
        "glucose_rows_without_time": scalar(f"SELECT count(*) FROM {source} WHERE {glucose} "
                                            f"AND {shared['glucose_time']} IS NULL"),
        "glucose_rows_outside_range": scalar(
            f"SELECT count(*) FROM {source} WHERE {glucose} AND lab_value_numeric IS NOT NULL "
            f"AND NOT ({glucose_filter(shared['glucose_categories'], 'lab_value_numeric', 'lab_category')})"),
        "glucose_rows_without_value": scalar(f"SELECT count(*) FROM {source} WHERE {glucose} "
                                             "AND lab_value_numeric IS NULL"),
    }


def daily_glucose(con, cfg: SiteConfig, decisions: dict) -> pd.DataFrame:
    """Per encounter, unit and local day: how many glucose results, their lowest, highest and mean."""
    shared = shared_values(decisions)
    placed = placed_sql(con, cfg, "labs", shared["glucose_time"], "s.lab_value_numeric AS glucose",
                        where=f"lower(CAST(s.lab_category AS VARCHAR)) IN ({quoted(shared['glucose_categories'])}) "
                              f"AND ({glucose_filter(shared['glucose_categories'])})")
    return frame_days(con.execute(f"""
        SELECT encounter_block, hospital, unit, day, count(*) AS glucose_results,
               min(glucose) AS glucose_min, max(glucose) AS glucose_max, avg(glucose) AS glucose_mean
        FROM ({placed}) GROUP BY ALL
    """).df())


def insulin_days(con, cfg: SiteConfig, decisions: dict) -> pd.DataFrame:
    """The ICU days on which an insulin infusion ran (DAY_KEY)."""
    if not cfg.table_path("medication_admin_continuous").exists():
        return pd.DataFrame(columns=DAY_KEY)
    agents = shared_values(decisions)["insulin_agents"]
    days = con.execute(interval_days_sql(infusion_runs_sql(con, cfg, {"insulin": agents}))).df()
    return frame_days(days)[DAY_KEY]


def build(site: SiteBuild) -> BuildResult:
    cfg, con = site.cfg, site.con
    tz = cfg.site_timezone
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    encounters = site.encounters
    adults = encounters[encounters["adult"]]

    intervals, unended = icu_intervals(site, shared["icu_locations"])
    days = days_present(intervals, tz)
    days = days[in_window(days["day"], cfg)].reset_index(drop=True)
    icu_encounters, icu_days = int(days["encounter_block"].nunique()), int(len(days))
    records = site.site_statistics.get("glucose_records", {})
    coverage = whole_months(records.get("first"), records.get("last"), records.get("first_admission"),
                            records.get("last_admission"), tz) if shared["glucose_coverage"] == "whole_months" else None
    if coverage is not None:
        days = days[(days["day"] >= coverage[0]) & (days["day"] <= coverage[1])].reset_index(drop=True)
        log(f"Glucose records and admissions cover whole months {coverage[0]:%Y-%m} to {coverage[1]:%Y-%m}; "
            f"{icu_days - len(days):,} ICU days outside them left out")

    with icu_session(con, cfg, intervals, site.mapping):
        glucose = daily_glucose(con, cfg, decisions)
        insulin = insulin_days(con, cfg, decisions)
    log(f"Glucose: {int(glucose['glucose_results'].sum()):,} results on {len(glucose):,} ICU days; "
        f"insulin infusions on {len(insulin):,} ICU days")

    days = days.merge(glucose, on=DAY_KEY, how="left").merge(insulin.assign(on_insulin=True), on=DAY_KEY, how="left")
    measured = days["glucose_results"].fillna(0) > 0
    on_insulin = days["on_insulin"].eq(True)
    low, high = threshold(decisions, "glucose_in_range")
    days["glucose_days"] = measured
    days["days_severe_hypo"] = measured & (days["glucose_min"] < threshold(decisions, "severe_hypoglycemia"))
    days["days_hypo"] = measured & (days["glucose_min"] < threshold(decisions, "hypoglycemia"))
    days["days_severe_hyper"] = measured & (days["glucose_max"] > threshold(decisions, "severe_hyperglycemia"))
    days["days_in_range"] = measured & days["glucose_mean"].between(low, high)
    days["insulin_days"] = on_insulin
    days["insulin_glucose_days"] = on_insulin & measured
    days["insulin_hypo_days"] = on_insulin & measured & (
        days["glucose_min"] < threshold(decisions, "hypoglycemia_on_insulin"))
    days[DAY_COUNTS] = days[DAY_COUNTS].astype(int)
    days["glucose_results"] = days["glucose_results"].fillna(0).astype(int)
    days["month"] = days["day"].dt.strftime("%Y-%m")
    rows = days.groupby(STAY, sort=True).agg(
        first_day=("day", "min"), last_day=("day", "max"), days_present=("day", "size"),
        glucose_results=("glucose_results", "sum"), glucose_lowest=("glucose_min", "min"),
        glucose_highest=("glucose_max", "max"), **{c: (c, "sum") for c in DAY_COUNTS}).reset_index()

    encounter_index = encounters.set_index("encounter_block")
    blocks = rows["encounter_block"]
    rows["label_id"] = blocks.map(encounter_index["hospitalization_id"])
    rows["sex"] = blocks.map(encounter_index["sex_category"]).fillna("Unknown")
    rows["race_eth"] = [prep.race_eth(encounter_index.at[b, "race_category"],
                                      encounter_index.at[b, "ethnicity_category"]) for b in blocks]
    rows["age_band"] = blocks.map(encounter_index["age"]).map(prep.age_band)
    rows["row_id"] = rows["label_id"] + " · " + rows["hospital"] + " · " + rows["unit"] + " · " + rows["month"]
    for column in ("first_day", "last_day"):
        rows[column] = rows[column].dt.strftime("%Y-%m-%d")
    rows = rows[["row_id", "encounter_block", "label_id", "month", "hospital", "unit", "sex", "race_eth", "age_band",
                 "first_day", "last_day", "days_present", "glucose_results", "glucose_lowest", "glucose_highest",
                 *DAY_COUNTS]]

    funnel = [
        {"step": "Encounters after linking", "n": int(len(encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with ICU time", "n": icu_encounters},
        {"step": "With ICU time in whole months of glucose records" if coverage is not None
         else "With ICU time (glucose-record coverage not applied)", "n": int(rows["encounter_block"].nunique())},
        {"step": "ICU stay-months (encounter, unit, month)", "n": int(len(rows)), "unit": "rows"},
        {"step": "Days present", "n": int(rows["days_present"].sum()), "unit": "days"},
        {"step": "Days with a glucose result", "n": int(rows["glucose_days"].sum()), "unit": "days"},
    ]
    log(f"{len(rows):,} stay-months; {int(rows['glucose_days'].sum()):,} ICU days with a glucose result")
    return BuildResult(
        tables={"stay_months": rows},
        summary={"funnel": funnel},
        meta={"months": sorted(rows["month"].unique().tolist())},
        data_quality={"icu_rows_without_end": unended,
                      "months_counted": None if coverage is None else
                      {"first": f"{coverage[0]:%Y-%m}", "last": f"{coverage[1]:%Y-%m}",
                       "icu_days_left_out": icu_days - int(rows["days_present"].sum())}},
    )
