"""Fluids build: input volume per ICU day, by CLIF 3.0 input group and category.

Every choice is in config/decisions/fluids.yaml. The `input` table can be large (a
row per charted volume), so DuckDB places each row in the ICU stay that contains
it and sums volumes per encounter, unit, local day and input category; only those
daily sums reach pandas. Time in the ICU per local day comes from ADT
(src/clif_dash/icu.py). Rows are one encounter's ICU time in one unit at one
hospital in one month, with a volume column per input group.
"""
from __future__ import annotations

import pandas as pd

from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, log
from ...icu import DAY_KEY, icu_intervals, icu_time_per_day, in_window, utc_text, whole_months
from ...upstream import prep

TABLES = ("input",)
REQUIRED_TABLES = ("input",)
STAY = ["encounter_block", "hospital", "unit", "month"]
# CLIF 3.0 input_group values (clifpy's 3.0 input schema), and the two IV categories shown on their own
GROUPS = ("iv_fluids", "blood_products", "nutrition", "medication_fluids", "renal_replacement_fluids",
          "flush_irrigation", "other")
IV_CATEGORIES = ("iv_crystalloid", "iv_colloid")
VOLUME_COLUMNS = ["vol_total", *[f"vol_{g}" for g in GROUPS], *[f"vol_{c}" for c in IV_CATEGORIES]]


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


def site_statistics(con, cfg: SiteConfig) -> dict:
    """When the site's input records begin and end, and the span of admissions."""
    first, last = con.execute(
        f"SELECT min(recorded_dttm), max(recorded_dttm) FROM read_parquet('{cfg.table_path('input')}')").fetchone()
    first_admission, last_admission = con.execute(
        f"SELECT min(admission_dttm), max(admission_dttm) FROM read_parquet('{cfg.table_path('hospitalization')}')"
    ).fetchone()
    return {"input_records": {"first": utc_text(first), "last": utc_text(last),
                              "first_admission": utc_text(first_admission), "last_admission": utc_text(last_admission)}}


def volume_limit_sql(limits: dict | None, group: str = "input_group") -> str:
    """SQL for the largest plausible single volume of a row's input group (volume_limits decision).

    No limit when the decision's value is null.
    """
    if not limits:
        return "CAST('infinity' AS DOUBLE)"
    cases = " ".join(f"WHEN '{g.lower()}' THEN {float(v)}" for g, v in limits.items() if g != "default")
    default = float(limits.get("default", float("inf")))
    return f"CASE lower(CAST({group} AS VARCHAR)) {cases} ELSE {default} END"


def data_quality(con, cfg: SiteConfig) -> dict:
    source = f"read_parquet('{cfg.table_path('input')}')"
    limit = volume_limit_sql(shared_values(cfg.decisions()).get("volume_limits"))

    def scalar(sql):
        return int(con.execute(sql).fetchone()[0] or 0)

    return {
        "input_rows_without_time": scalar(f"SELECT count(*) FROM {source} WHERE recorded_dttm IS NULL"),
        "input_rows_without_positive_volume": scalar(
            f"SELECT count(*) FROM {source} WHERE input_volume IS NULL OR input_volume <= 0"),
        "input_rows_over_limit": {str(k): {"rows": int(n), "litres": float(litres)} for k, n, litres in con.execute(
            f"SELECT lower(CAST(input_group AS VARCHAR)), count(*), sum(input_volume) / 1000 FROM {source} "
            f"WHERE input_volume > {limit} GROUP BY 1 ORDER BY 2 DESC").fetchall()},
        "input_rows_by_group": {str(k): int(n) for k, n in con.execute(
            f"SELECT lower(CAST(input_group AS VARCHAR)), count(*) FROM {source} GROUP BY 1 ORDER BY 2 DESC").fetchall()},
    }


def utc_timestamp(con, source: str, column: str) -> str:
    """SQL for a CLIF datetime column as a UTC timestamp with time zone, however the site stored it.

    CLIF datetimes are UTC: a parquet timestamp without a zone is read as UTC, not as the
    session's zone, and text is parsed with its offset.
    """
    name = column.split(".")[-1]
    kind = con.execute(f"SELECT column_type FROM (DESCRIBE SELECT {name} FROM {source})").fetchone()[0].upper()
    if "TIME ZONE" in kind:
        return column
    if kind.startswith("TIMESTAMP"):
        return f"timezone('UTC', {column})"
    return f"CAST({column} AS TIMESTAMPTZ)"


def daily_volumes(con, cfg: SiteConfig, intervals: pd.DataFrame, mapping: pd.DataFrame,
                  limits: dict | None) -> pd.DataFrame:
    """mL per encounter, unit, local day and input category, for volumes recorded in an ICU.

    A volume in two overlapping ICU rows counts once, in the earlier one; a volume above
    its group's limit (volume_limits) does not count.
    """
    source = f"read_parquet('{cfg.table_path('input')}', file_row_number = true)"
    recorded = utc_timestamp(con, source, "s.recorded_dttm")
    con.register("icu_rows", intervals)
    con.register("encounter_map", mapping[["hospitalization_id", "encounter_block"]])
    con.execute(f"SET TimeZone = '{cfg.site_timezone}'")
    try:
        frame = con.execute(f"""
            WITH placed AS (
                SELECT i.encounter_block, i.hospital, i.unit, {recorded} AS recorded_dttm, s.input_volume,
                       lower(CAST(s.input_group AS VARCHAR)) AS input_group,
                       lower(CAST(s.input_category AS VARCHAR)) AS input_category
                FROM {source} AS s
                JOIN encounter_map AS m ON CAST(s.hospitalization_id AS VARCHAR) = m.hospitalization_id
                JOIN icu_rows AS i ON i.encounter_block = m.encounter_block
                     AND i.in_dttm <= {recorded} AND {recorded} < i.out_dttm
                WHERE s.input_volume > 0 AND s.recorded_dttm IS NOT NULL
                      AND s.input_volume <= {volume_limit_sql(limits, "s.input_group")}
                QUALIFY row_number() OVER (PARTITION BY s.file_row_number ORDER BY i.in_dttm) = 1
            )
            SELECT encounter_block, hospital, unit, CAST(recorded_dttm AS DATE) AS day,
                   input_group, input_category, sum(input_volume) AS volume
            FROM placed GROUP BY ALL
        """).df()
    finally:
        con.execute("SET TimeZone = 'UTC'")
        con.unregister("icu_rows")
        con.unregister("encounter_map")
    frame["day"] = pd.to_datetime(frame["day"])
    return frame


def build(site: SiteBuild) -> BuildResult:
    cfg, con = site.cfg, site.con
    tz = cfg.site_timezone
    shared = shared_values(cfg.decisions())
    encounters = site.encounters
    adults = encounters[encounters["adult"]]

    intervals, unended = icu_intervals(site, shared["icu_locations"])
    days = icu_time_per_day(intervals, tz)
    days = days[in_window(days["day"], cfg)].reset_index(drop=True)
    icu_encounters, icu_days = int(days["encounter_block"].nunique()), int(len(days))
    records = site.site_statistics.get("input_records", {})
    coverage = whole_months(records.get("first"), records.get("last"), records.get("first_admission"),
                            records.get("last_admission"), tz) if shared["input_coverage"] == "whole_months" else None
    if coverage is not None:
        days = days[(days["day"] >= coverage[0]) & (days["day"] <= coverage[1])].reset_index(drop=True)
        log(f"Input records and admissions cover whole months {coverage[0]:%Y-%m} to {coverage[1]:%Y-%m}; "
            f"{icu_days - len(days):,} ICU days outside them left out")

    volumes = daily_volumes(con, cfg, intervals, site.mapping, shared.get("volume_limits"))
    log(f"Input: {volumes['volume'].sum() / 1000:,.0f} L recorded in ICUs across "
        f"{volumes['encounter_block'].nunique():,} encounters")
    by_group = volumes.pivot_table(index=DAY_KEY, columns="input_group", values="volume", aggfunc="sum")
    by_category = volumes[volumes["input_category"].isin(IV_CATEGORIES)].pivot_table(
        index=DAY_KEY, columns="input_category", values="volume", aggfunc="sum")
    daily = pd.concat([by_group.reindex(columns=list(GROUPS)).add_prefix("vol_"),
                       by_category.reindex(columns=list(IV_CATEGORIES)).add_prefix("vol_"),
                       volumes.groupby(DAY_KEY)["volume"].sum().rename("vol_total")], axis=1).reset_index()
    days = days.merge(daily, on=DAY_KEY, how="left") if len(daily) else days
    days = days.reindex(columns=[*days.columns, *[c for c in VOLUME_COLUMNS if c not in days.columns]])
    days[VOLUME_COLUMNS] = days[VOLUME_COLUMNS].astype(float).fillna(0.0)
    days["month"] = days["day"].dt.strftime("%Y-%m")
    rows = days.groupby(STAY, sort=True).agg(
        first_day=("day", "min"), last_day=("day", "max"), days_present=("day", "size"), icu_hours=("hours", "sum"),
        **{c: (c, "sum") for c in VOLUME_COLUMNS}).reset_index()
    rows["icu_days"] = rows["icu_hours"] / 24

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
                 "first_day", "last_day", "days_present", "icu_days", *VOLUME_COLUMNS]]

    funnel = [
        {"step": "Encounters after linking", "n": int(len(encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with ICU time", "n": icu_encounters},
        {"step": "With ICU time in whole months of input records" if coverage is not None
         else "With ICU time (input-record coverage not applied)", "n": int(rows["encounter_block"].nunique())},
        {"step": "ICU stay-months (encounter, unit, month)", "n": int(len(rows)), "unit": "rows"},
    ]
    log(f"{len(rows):,} stay-months; {rows['icu_days'].sum():,.0f} ICU days")
    return BuildResult(
        tables={"stay_months": rows},
        summary={"funnel": funnel},
        meta={"months": sorted(rows["month"].unique().tolist())},
        data_quality={"icu_rows_without_end": unended,
                      "months_counted": None if coverage is None else
                      {"first": f"{coverage[0]:%Y-%m}", "last": f"{coverage[1]:%Y-%m}",
                       "icu_days_left_out": icu_days - int(rows["days_present"].sum())}},
    )
