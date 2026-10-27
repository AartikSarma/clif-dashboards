"""ICU Liberation build: pain, sedation, delirium, SAT and SBT per ICU day, by ICU and month.

Every choice is in config/decisions/liberation.yaml. Days present come from ADT
(src/clif_dash/icu.py). DuckDB places each assessment in the ICU stay that
contains it and summarizes it per encounter, unit and local day; continuous
sedative, benzodiazepine and paralytic infusions become the ICU days on which
one ran, and respiratory_support rows the ICU days on invasive ventilation.
Only those daily summaries reach pandas. Rows are one encounter's days in one
unit at one hospital in one month.

SAT and SBT here are documented trials only, counted per ICU day. The
respiratory dashboard also counts trials found from the EHR (sedatives
stopped, pressure support lowered) on its own vent-days, so its figures differ.
"""
from __future__ import annotations

import pandas as pd

from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, log
from ...icu import (DAY_KEY, days_present, frame_days, icu_intervals, icu_session, in_window, infusion_runs_sql,
                    interval_days_sql, outlier_bounds, placed_sql, quoted, utc_text, whole_months)
from ...upstream import prep

TABLES = ("patient_assessments", "medication_admin_continuous", "respiratory_support")
REQUIRED_TABLES = ("patient_assessments",)
STAY = ["encounter_block", "hospital", "unit", "month"]
INFUSIONS = ("sedative", "benzodiazepine", "paralytic")
DAY_COUNTS = ["pain_days", "rass_days", "rass_readings", "rass_in_range", "rass_scored_days", "deep_days",
              "cam_days", "cam_result_days", "cam_positive_days", "sedative_days", "benzodiazepine_days",
              "imv_days", "sat_eligible_days", "sat_days", "sbt_days"]


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


def metric_values(decisions: dict, measure: str) -> dict:
    return {d["id"]: d["value"] for d in decisions["metrics"][measure]["decisions"]}


def assessment_categories(decisions: dict) -> dict[str, list[str]]:
    """The assessment_category values each part of the build reads, lower-cased."""
    shared = shared_values(decisions)
    lowered = lambda values: [v.lower() for v in values]
    return {"rass": lowered(shared["rass_categories"]), "cam": lowered(shared["cam_categories"]),
            "pain": lowered(metric_values(decisions, "pain_assessed")["pain_categories"]),
            "sat": lowered(metric_values(decisions, "sat")["documented"]),
            "sbt": lowered(metric_values(decisions, "sbt")["documented"])}


def site_statistics(con, cfg: SiteConfig) -> dict:
    """When the site's RASS, CAM-ICU and pain assessments begin and end, and the span of admissions."""
    categories = assessment_categories(cfg.decisions())
    tracked = categories["rass"] + categories["cam"] + categories["pain"]
    first, last = con.execute(
        f"SELECT min(recorded_dttm), max(recorded_dttm) FROM read_parquet('{cfg.table_path('patient_assessments')}') "
        f"WHERE lower(CAST(assessment_category AS VARCHAR)) IN ({quoted(tracked)})").fetchone()
    first_admission, last_admission = con.execute(
        f"SELECT min(admission_dttm), max(admission_dttm) FROM read_parquet('{cfg.table_path('hospitalization')}')"
    ).fetchone()
    return {"assessment_records": {"first": utc_text(first), "last": utc_text(last),
                                   "first_admission": utc_text(first_admission),
                                   "last_admission": utc_text(last_admission)}}


def data_quality(con, cfg: SiteConfig) -> dict:
    categories = assessment_categories(cfg.decisions())
    source = f"read_parquet('{cfg.table_path('patient_assessments')}')"
    every = categories["rass"] + categories["cam"] + categories["pain"] + categories["sat"] + categories["sbt"]
    by_category = {str(k): int(n) for k, n in con.execute(
        f"SELECT lower(CAST(assessment_category AS VARCHAR)), count(*) FROM {source} "
        f"WHERE lower(CAST(assessment_category AS VARCHAR)) IN ({quoted(every)}) GROUP BY 1 ORDER BY 2 DESC").fetchall()}
    cam_values = {str(k): int(n) for k, n in con.execute(
        f"SELECT coalesce(lower(CAST(categorical_value AS VARCHAR)), 'numeric ' || CAST(numerical_value AS VARCHAR), "
        f"'(none)'), count(*) FROM {source} WHERE lower(CAST(assessment_category AS VARCHAR)) "
        f"IN ({quoted(categories['cam'])}) GROUP BY 1 ORDER BY 2 DESC LIMIT 20").fetchall()}
    return {"assessment_rows_by_category": by_category, "cam_values": cam_values,
            "documented_sat_rows": sum(by_category.get(c, 0) for c in categories["sat"]),
            "documented_sbt_rows": sum(by_category.get(c, 0) for c in categories["sbt"])}


def documented(categories: list[str], negatives: list[str]) -> str:
    """SQL: a documented trial, any row of the categories unless its value says it was not done."""
    return (f"(category IN ({quoted(categories)}) AND NOT coalesce(categorical IN ({quoted(negatives)}), FALSE) "
            "AND NOT (categorical IS NULL AND coalesce(numerical = 0, FALSE)))")


def daily_assessments(con, cfg: SiteConfig, decisions: dict, paralytic_runs: str) -> pd.DataFrame:
    """Per encounter, unit and local day: pain, RASS, CAM-ICU and documented SAT and SBT."""
    categories = assessment_categories(decisions)
    shared = shared_values(decisions)
    low, high = metric_values(decisions, "light_sedation")["target_range"]
    deep = metric_values(decisions, "deep_sedation")["threshold"]
    negatives = [v.lower() for v in shared["not_done_values"]]
    every = categories["rass"] + categories["cam"] + categories["pain"] + categories["sat"] + categories["sbt"]
    rass_low, rass_high = outlier_bounds("patient_assessments", "numerical_value", "RASS")
    placed = placed_sql(con, cfg, "patient_assessments", "recorded_dttm",
                        "lower(CAST(s.assessment_category AS VARCHAR)) AS category, "
                        "TRY_CAST(s.numerical_value AS DOUBLE) AS numerical, "
                        "lower(trim(CAST(s.categorical_value AS VARCHAR))) AS categorical",
                        where=f"lower(CAST(s.assessment_category AS VARCHAR)) IN ({quoted(every)})")
    positive = "(categorical = 'positive' OR (categorical IS NULL AND numerical >= 1))"
    negative = "(categorical = 'negative' OR (categorical IS NULL AND numerical = 0))"
    return frame_days(con.execute(f"""
        WITH placed AS ({placed}),
        rass AS (
            SELECT p.*, EXISTS (SELECT 1 FROM ({paralytic_runs}) AS r WHERE r.encounter_block = p.encounter_block
                                AND r.start_dttm <= p.event_dttm AND p.event_dttm < r.end_dttm) AS paralyzed
            FROM placed AS p
            WHERE category IN ({quoted(categories['rass'])}) AND numerical BETWEEN {rass_low} AND {rass_high}
        ),
        rass_days AS (
            SELECT encounter_block, hospital, unit, day, count(*) FILTER (WHERE NOT paralyzed) AS rass_readings,
                   count(*) FILTER (WHERE NOT paralyzed AND numerical BETWEEN {low} AND {high}) AS rass_in_range,
                   count(*) FILTER (WHERE NOT paralyzed AND numerical <= {deep}) AS rass_deep
            FROM rass GROUP BY ALL
        ),
        other_days AS (
            SELECT encounter_block, hospital, unit, day,
                   bool_or(category IN ({quoted(categories['pain'])})
                           AND (numerical IS NOT NULL OR categorical IS NOT NULL)) AS pain,
                   bool_or(category IN ({quoted(categories['cam'])})
                           AND (numerical IS NOT NULL OR categorical IS NOT NULL)) AS cam,
                   bool_or(category IN ({quoted(categories['cam'])}) AND ({positive} OR {negative})) AS cam_result,
                   bool_or(category IN ({quoted(categories['cam'])}) AND {positive}) AS cam_positive,
                   bool_or({documented(categories['sat'], negatives)}) AS sat,
                   bool_or({documented(categories['sbt'], negatives)}) AS sbt
            FROM placed GROUP BY ALL
        )
        SELECT o.*, coalesce(r.rass_readings, 0) AS rass_readings, coalesce(r.rass_in_range, 0) AS rass_in_range,
               coalesce(r.rass_deep, 0) AS rass_deep, r.encounter_block IS NOT NULL AS rass
        FROM other_days AS o
        LEFT JOIN rass_days AS r USING (encounter_block, hospital, unit, day)
    """).df())


def infusion_days(con, cfg: SiteConfig, decisions: dict) -> tuple[pd.DataFrame, str]:
    """The ICU days on which each kind of infusion ran (DAY_KEY + kind), and SQL for paralytic intervals."""
    shared = shared_values(decisions)
    agents = {"sedative": shared["sedatives"], "benzodiazepine": shared["benzodiazepines"],
              "paralytic": shared["paralytics"]}
    if not cfg.table_path("medication_admin_continuous").exists():
        empty = "SELECT NULL AS encounter_block, NULL AS start_dttm, NULL AS end_dttm WHERE FALSE"
        return pd.DataFrame(columns=DAY_KEY + ["kind"]), empty
    runs = {kind: infusion_runs_sql(con, cfg, {kind: agents[kind]}) for kind in INFUSIONS}
    days = pd.concat([con.execute(interval_days_sql(runs[kind])).df() for kind in INFUSIONS], ignore_index=True)
    return frame_days(days), runs["paralytic"]


def ventilated_days(con, cfg: SiteConfig, decisions: dict) -> pd.DataFrame:
    """The ICU days with any respiratory_support row on invasive ventilation (DAY_KEY)."""
    if not cfg.table_path("respiratory_support").exists():
        return pd.DataFrame(columns=DAY_KEY)
    devices = shared_values(decisions)["imv_devices"]
    placed = placed_sql(con, cfg, "respiratory_support", "recorded_dttm", "1 AS ventilated",
                        where=f"lower(CAST(s.device_category AS VARCHAR)) IN ({quoted(devices)})")
    return frame_days(con.execute(f"SELECT DISTINCT encounter_block, hospital, unit, day FROM ({placed})").df())


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
    records = site.site_statistics.get("assessment_records", {})
    coverage = whole_months(records.get("first"), records.get("last"), records.get("first_admission"),
                            records.get("last_admission"), tz) \
        if shared["assessment_coverage"] == "whole_months" else None
    if coverage is not None:
        days = days[(days["day"] >= coverage[0]) & (days["day"] <= coverage[1])].reset_index(drop=True)
        log(f"Assessments and admissions cover whole months {coverage[0]:%Y-%m} to {coverage[1]:%Y-%m}; "
            f"{icu_days - len(days):,} ICU days outside them left out")

    with icu_session(con, cfg, intervals, site.mapping):
        infusions, paralytic_runs = infusion_days(con, cfg, decisions)
        assessments = daily_assessments(con, cfg, decisions, paralytic_runs)
        ventilated = ventilated_days(con, cfg, decisions)
    log(f"Assessments on {len(assessments):,} ICU days; infusions on {infusions[DAY_KEY].drop_duplicates().shape[0]:,}; "
        f"invasive ventilation on {len(ventilated):,}")

    days = days.merge(assessments, on=DAY_KEY, how="left")
    for kind in INFUSIONS:
        on = infusions.loc[infusions["kind"] == kind, DAY_KEY].assign(**{f"on_{kind}": True})
        days = days.merge(on, on=DAY_KEY, how="left")
    days = days.merge(ventilated.assign(on_imv=True), on=DAY_KEY, how="left")
    def flag(column):
        if column not in days:
            return pd.Series(False, index=days.index)
        return days[column].astype("boolean").fillna(False).astype(bool)

    sedated, paralyzed, ventilated_day = flag("on_sedative"), flag("on_paralytic"), flag("on_imv")
    for column in ("rass_readings", "rass_in_range", "rass_deep"):
        days[column] = days[column].fillna(0)
    days["pain_days"] = flag("pain")
    days["rass_days"] = flag("rass")
    days["rass_scored_days"] = days["rass_readings"] > 0
    days["deep_days"] = days["rass_scored_days"] & (days["rass_deep"] > days["rass_readings"] / 2)
    days["cam_days"] = flag("cam")
    days["cam_result_days"] = flag("cam_result")
    days["cam_positive_days"] = flag("cam_positive")
    days["sedative_days"] = sedated
    days["benzodiazepine_days"] = flag("on_benzodiazepine")
    days["imv_days"] = ventilated_day
    days["sat_eligible_days"] = ventilated_day & sedated & ~paralyzed
    days["sat_days"] = days["sat_eligible_days"] & flag("sat")
    days["sbt_days"] = ventilated_day & flag("sbt")
    days[DAY_COUNTS] = days[DAY_COUNTS].astype(int)
    days["month"] = days["day"].dt.strftime("%Y-%m")
    rows = days.groupby(STAY, sort=True).agg(
        first_day=("day", "min"), last_day=("day", "max"), days_present=("day", "size"),
        **{c: (c, "sum") for c in DAY_COUNTS}).reset_index()

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
                 "first_day", "last_day", "days_present", *DAY_COUNTS]]

    funnel = [
        {"step": "Encounters after linking", "n": int(len(encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with ICU time", "n": icu_encounters},
        {"step": "With ICU time in whole months of assessment records" if coverage is not None
         else "With ICU time (assessment-record coverage not applied)", "n": int(rows["encounter_block"].nunique())},
        {"step": "ICU stay-months (encounter, unit, month)", "n": int(len(rows)), "unit": "rows"},
        {"step": "Days present", "n": int(rows["days_present"].sum()), "unit": "days"},
        {"step": "Days on invasive ventilation", "n": int(rows["imv_days"].sum()), "unit": "days"},
    ]
    log(f"{len(rows):,} stay-months; {int(rows['days_present'].sum()):,} days present")
    return BuildResult(
        tables={"stay_months": rows},
        summary={"funnel": funnel},
        meta={"months": sorted(rows["month"].unique().tolist())},
        data_quality={"icu_rows_without_end": unended,
                      "months_counted": None if coverage is None else
                      {"first": f"{coverage[0]:%Y-%m}", "last": f"{coverage[1]:%Y-%m}",
                       "icu_days_left_out": icu_days - int(rows["days_present"].sum())}},
    )
