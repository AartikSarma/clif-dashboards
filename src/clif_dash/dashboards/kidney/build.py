"""Kidney build: acute kidney injury arising in the ICU, creatinine monitoring and CRRT, by ICU and month.

Every choice is in config/decisions/kidney.yaml. Days present come from ADT
(src/clif_dash/icu.py). DuckDB does the rest, so only per-day summaries reach
pandas:

1. Each encounter's creatinine results, in time order, are compared with the
   lowest result in the 48 h and 7 days before them (KDIGO's creatinine
   criteria). The first result meeting stage 1, and the first meeting stage 2
   or 3, are each the encounter's onset; an onset counts as arising in the ICU
   when it falls in an ICU stay from the third ICU day on.
2. crrt_therapy rows are placed in ICU days; a day's effluent dose is the mean
   of its rows' summed flow rates divided by the patient's weight (vitals).
"""
from __future__ import annotations

import pandas as pd

from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, log
from ...icu import (DAY_KEY, days_present, frame_days, icu_intervals, icu_session, in_window, outlier_bounds,
                    placed_sql, quoted, source_sql, utc_text, utc_timestamp, whole_months)
from ...upstream import clif_io, prep

TABLES = ("labs", "crrt_therapy", "vitals")
REQUIRED_TABLES = ("labs",)
STAY = ["encounter_block", "hospital", "unit", "month"]
TIME_COLUMNS = ("lab_collect_dttm", "lab_result_dttm")
CRRT_FLOWS = ("pre_filter_replacement_fluid_rate", "post_filter_replacement_fluid_rate", "dialysate_flow_rate",
              "ultrafiltration_out")
DAY_COUNTS = ["creatinine_days", "aki_events", "severe_aki_events", "crrt_days", "crrt_dose_days",
              "crrt_dose_target_days"]


def shared_values(decisions: dict) -> dict:
    shared = {d["id"]: d["value"] for d in decisions["shared"]}
    if shared["creatinine_time"] not in TIME_COLUMNS:
        raise ValueError(f"creatinine_time must be one of {TIME_COLUMNS}, not {shared['creatinine_time']!r}")
    return shared


def metric_values(decisions: dict, measure: str) -> dict:
    return {d["id"]: d["value"] for d in decisions["metrics"][measure]["decisions"]}


def creatinine_where(category: str = "s.lab_category", value: str = "s.lab_value_numeric") -> str:
    low, high = outlier_bounds("labs", "lab_value_numeric", "creatinine")
    return f"lower(CAST({category} AS VARCHAR)) = 'creatinine' AND {value} > 0 AND {value} BETWEEN {low} AND {high}"


def site_statistics(con, cfg: SiteConfig) -> dict:
    """When the site's creatinine results begin and end, and the span of admissions."""
    time = shared_values(cfg.decisions())["creatinine_time"]
    first, last = con.execute(
        f"SELECT min({time}), max({time}) FROM read_parquet('{cfg.table_path('labs')}') "
        "WHERE lower(CAST(lab_category AS VARCHAR)) = 'creatinine'").fetchone()
    first_admission, last_admission = con.execute(
        f"SELECT min(admission_dttm), max(admission_dttm) FROM read_parquet('{cfg.table_path('hospitalization')}')"
    ).fetchone()
    return {"creatinine_records": {"first": utc_text(first), "last": utc_text(last),
                                   "first_admission": utc_text(first_admission),
                                   "last_admission": utc_text(last_admission)}}


def data_quality(con, cfg: SiteConfig) -> dict:
    labs = f"read_parquet('{cfg.table_path('labs')}')"
    creatinine = "lower(CAST(lab_category AS VARCHAR)) = 'creatinine'"

    def scalar(sql):
        return int(con.execute(sql).fetchone()[0] or 0)

    quality = {
        "creatinine_rows_by_unit": {str(k): int(n) for k, n in con.execute(
            f"SELECT coalesce(CAST(reference_unit AS VARCHAR), '(none)'), count(*) FROM {labs} WHERE {creatinine} GROUP BY 1 "
            "ORDER BY 2 DESC").fetchall()},
        "creatinine_rows_outside_range": scalar(
            f"SELECT count(*) FROM {labs} WHERE {creatinine} AND lab_value_numeric IS NOT NULL "
            f"AND NOT ({creatinine_where('lab_category', 'lab_value_numeric')})"),
    }
    if cfg.table_path("crrt_therapy").exists():
        crrt = f"read_parquet('{cfg.table_path('crrt_therapy')}')"
        flows = [c for c in CRRT_FLOWS if c in set(con.execute(f"SELECT * FROM {crrt} LIMIT 0").df().columns)]
        quality["crrt_rows_without_flows"] = scalar(
            f"SELECT count(*) FROM {crrt} WHERE " + (" AND ".join(f"{c} IS NULL" for c in flows) or "TRUE"))
    return quality


def creatinine_onsets(con, cfg: SiteConfig, decisions: dict) -> pd.DataFrame:
    """Each encounter's first creatinine result meeting KDIGO stage 1, and its first meeting stage 2 or 3,
    placed in the ICU stay containing it (onsets outside an ICU are dropped). Gives DAY_KEY + `severity`."""
    shared = shared_values(decisions)
    criteria = metric_values(decisions, "aki_icu_onset")
    source = source_sql(cfg, "labs")
    time = utc_timestamp(con, source, f"s.{shared['creatinine_time']}")
    rise, rise_hours = criteria["absolute_rise"], criteria["absolute_rise_hours"]
    ratio, baseline_days = criteria["relative_rise"], criteria["baseline_days"]
    stage2, stage3, stage3_value = criteria["stage2_relative_rise"], criteria["stage3_relative_rise"], \
        criteria["stage3_value"]
    onset_day = criteria["onset_icu_day"]
    return frame_days(con.execute(f"""
        WITH results AS (
            SELECT m.encounter_block, {time} AS event_dttm, s.lab_value_numeric AS value, s.file_row_number
            FROM {source} AS s
            JOIN encounter_map AS m ON CAST(s.hospitalization_id AS VARCHAR) = CAST(m.hospitalization_id AS VARCHAR)
            WHERE s.{shared['creatinine_time']} IS NOT NULL AND {creatinine_where()}
        ),
        compared AS (
            SELECT *, min(value) OVER (PARTITION BY encounter_block ORDER BY event_dttm
                                       RANGE BETWEEN INTERVAL {int(rise_hours)} HOURS PRECEDING AND CURRENT ROW) AS low_48h,
                      min(value) OVER (PARTITION BY encounter_block ORDER BY event_dttm
                                       RANGE BETWEEN INTERVAL {int(baseline_days)} DAYS PRECEDING AND CURRENT ROW) AS low_7d
            FROM results
        ),
        staged AS (
            SELECT *, value - low_48h >= {rise} - 1e-9 OR value >= {ratio} * low_7d - 1e-9 AS stage1,
                      value >= {stage2} * low_7d - 1e-9 OR value >= {stage3} * low_7d - 1e-9
                      OR (value >= {stage3_value} AND (value - low_48h >= {rise} - 1e-9
                                                       OR value >= {ratio} * low_7d - 1e-9)) AS stage2_or_3
            FROM compared
        ),
        onsets AS (
            SELECT encounter_block, 'any' AS severity, event_dttm FROM staged WHERE stage1
            QUALIFY row_number() OVER (PARTITION BY encounter_block ORDER BY event_dttm, file_row_number) = 1
            UNION ALL
            SELECT encounter_block, 'severe' AS severity, event_dttm FROM staged WHERE stage2_or_3
            QUALIFY row_number() OVER (PARTITION BY encounter_block ORDER BY event_dttm, file_row_number) = 1
        ),
        first_icu AS (SELECT encounter_block, CAST(min(in_dttm) AS DATE) AS first_day FROM icu_rows GROUP BY 1)
        SELECT i.encounter_block, i.hospital, i.unit, CAST(o.event_dttm AS DATE) AS day, o.severity
        FROM onsets AS o
        JOIN icu_rows AS i ON i.encounter_block = o.encounter_block
             AND i.in_dttm <= o.event_dttm AND o.event_dttm < i.out_dttm
        JOIN first_icu AS f ON f.encounter_block = o.encounter_block
        WHERE CAST(o.event_dttm AS DATE) - f.first_day >= {int(onset_day) - 1}
        QUALIFY row_number() OVER (PARTITION BY o.encounter_block, o.severity ORDER BY i.in_dttm) = 1
    """).df())


def creatinine_days(con, cfg: SiteConfig, decisions: dict) -> pd.DataFrame:
    """The ICU days with a creatinine result (DAY_KEY)."""
    time = shared_values(decisions)["creatinine_time"]
    placed = placed_sql(con, cfg, "labs", time, "1 AS measured", where=creatinine_where())
    return frame_days(con.execute(f"SELECT DISTINCT encounter_block, hospital, unit, day FROM ({placed})").df())


def crrt_days(con, cfg: SiteConfig, decisions: dict) -> pd.DataFrame:
    """Per ICU day on CRRT: the mean effluent dose in mL/kg/h, where flows and weight are known (DAY_KEY + dose)."""
    if not cfg.table_path("crrt_therapy").exists():
        return pd.DataFrame(columns=DAY_KEY + ["dose"])
    ranges = clif_io.outlier_config().get("crrt_therapy", {})
    flows = []
    for column in CRRT_FLOWS:
        low, high = ranges.get(column, {}).get("min", 0), ranges.get(column, {}).get("max", 1e9)
        flows.append(f"CASE WHEN TRY_CAST(s.{column} AS DOUBLE) BETWEEN {low} AND {high} "
                     f"THEN TRY_CAST(s.{column} AS DOUBLE) END")
    flow_columns = ", ".join(f"{sql} AS {name}" for sql, name in zip(flows, CRRT_FLOWS))
    placed = placed_sql(con, cfg, "crrt_therapy", "recorded_dttm", flow_columns)
    effluent = " + ".join(f"coalesce({c}, 0)" for c in CRRT_FLOWS)
    any_flow = " OR ".join(f"{c} IS NOT NULL" for c in CRRT_FLOWS)
    weights = "SELECT NULL AS encounter_block, NULL AS weight_kg WHERE FALSE"
    if cfg.table_path("vitals").exists():
        low, high = outlier_bounds("vitals", "vital_value", "weight_kg")
        vitals = source_sql(cfg, "vitals")
        weights = f"""
            SELECT m.encounter_block, median(TRY_CAST(s.vital_value AS DOUBLE)) AS weight_kg
            FROM {vitals} AS s
            JOIN encounter_map AS m ON CAST(s.hospitalization_id AS VARCHAR) = CAST(m.hospitalization_id AS VARCHAR)
            WHERE lower(CAST(s.vital_category AS VARCHAR)) = 'weight_kg'
              AND TRY_CAST(s.vital_value AS DOUBLE) BETWEEN {low} AND {high}
              AND m.encounter_block IN (SELECT DISTINCT encounter_block FROM placed)
            GROUP BY 1
        """
    return frame_days(con.execute(f"""
        WITH placed AS ({placed}), weights AS ({weights})
        SELECT p.encounter_block, p.hospital, p.unit, p.day,
               avg(CASE WHEN {any_flow} THEN ({effluent}) END) / any_value(w.weight_kg) AS dose
        FROM placed AS p LEFT JOIN weights AS w USING (encounter_block)
        GROUP BY ALL
    """).df())


def build(site: SiteBuild) -> BuildResult:
    cfg, con = site.cfg, site.con
    tz = cfg.site_timezone
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    target = metric_values(decisions, "crrt_dose")["target"]
    encounters = site.encounters
    adults = encounters[encounters["adult"]]

    intervals, unended = icu_intervals(site, shared["icu_locations"])
    days = days_present(intervals, tz)
    days = days[in_window(days["day"], cfg)].reset_index(drop=True)
    icu_encounters, icu_days = int(days["encounter_block"].nunique()), int(len(days))
    records = site.site_statistics.get("creatinine_records", {})
    coverage = whole_months(records.get("first"), records.get("last"), records.get("first_admission"),
                            records.get("last_admission"), tz) \
        if shared["creatinine_coverage"] == "whole_months" else None
    if coverage is not None:
        days = days[(days["day"] >= coverage[0]) & (days["day"] <= coverage[1])].reset_index(drop=True)
        log(f"Creatinine records and admissions cover whole months {coverage[0]:%Y-%m} to {coverage[1]:%Y-%m}; "
            f"{icu_days - len(days):,} ICU days outside them left out")

    with icu_session(con, cfg, intervals, site.mapping):
        onsets = creatinine_onsets(con, cfg, decisions)
        measured = creatinine_days(con, cfg, decisions)
        crrt = crrt_days(con, cfg, decisions)
    log(f"AKI arising in the ICU: {int((onsets['severity'] == 'any').sum()):,} encounters "
        f"({int((onsets['severity'] == 'severe').sum()):,} stage 2 or 3); CRRT on {len(crrt):,} ICU days")

    for severity, column in (("any", "aki_events"), ("severe", "severe_aki_events")):
        events = onsets.loc[onsets["severity"] == severity, DAY_KEY].assign(**{column: 1})
        days = days.merge(events, on=DAY_KEY, how="left")
    days = days.merge(measured.assign(creatinine_days=1), on=DAY_KEY, how="left")
    days = days.merge(crrt.assign(crrt_days=1), on=DAY_KEY, how="left")
    days["crrt_dose_days"] = days["dose"].notna()
    days["crrt_dose_target_days"] = days["dose"] >= target
    days[DAY_COUNTS] = days[DAY_COUNTS].fillna(0).astype(int)
    days["month"] = days["day"].dt.strftime("%Y-%m")
    rows = days.groupby(STAY, sort=True).agg(
        first_day=("day", "min"), last_day=("day", "max"), days_present=("day", "size"),
        crrt_dose_mean=("dose", "mean"), **{c: (c, "sum") for c in DAY_COUNTS}).reset_index()
    rows["crrt_dose_mean"] = rows["crrt_dose_mean"].round(1)

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
                 "first_day", "last_day", "days_present", *DAY_COUNTS, "crrt_dose_mean"]]

    funnel = [
        {"step": "Encounters after linking", "n": int(len(encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with ICU time", "n": icu_encounters},
        {"step": "With ICU time in whole months of creatinine records" if coverage is not None
         else "With ICU time (creatinine-record coverage not applied)", "n": int(rows["encounter_block"].nunique())},
        {"step": "ICU stay-months (encounter, unit, month)", "n": int(len(rows)), "unit": "rows"},
        {"step": "Days present", "n": int(rows["days_present"].sum()), "unit": "days"},
        {"step": "Days on CRRT", "n": int(rows["crrt_days"].sum()), "unit": "days"},
    ]
    log(f"{len(rows):,} stay-months; {int(rows['aki_events'].sum()):,} AKI onsets in the ICU counted")
    return BuildResult(
        tables={"stay_months": rows},
        summary={"funnel": funnel},
        meta={"months": sorted(rows["month"].unique().tolist())},
        data_quality={"icu_rows_without_end": unended,
                      "crrt_days_without_dose": int(rows["crrt_days"].sum() - rows["crrt_dose_days"].sum()),
                      "months_counted": None if coverage is None else
                      {"first": f"{coverage[0]:%Y-%m}", "last": f"{coverage[1]:%Y-%m}",
                       "icu_days_left_out": icu_days - int(rows["days_present"].sum())}},
    )
