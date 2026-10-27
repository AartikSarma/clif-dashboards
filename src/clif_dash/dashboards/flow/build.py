"""ICU flow build: one row per adult encounter with ICU time, from ADT alone.

Every choice is in config/decisions/flow.yaml. ADT is small, so everything runs
in the main process. Each encounter is attributed to its first ICU row: the
unit, hospital and month of ICU admission. The readmission and length-of-stay
rules are the respiratory dashboard's (upstream metrics.py), applied to every
ICU encounter rather than only ventilated ones.
"""
from __future__ import annotations

import pandas as pd

from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, log
from ...icu import icu_intervals, in_window, lower, utc_text, whole_months
from ...upstream import prep

TABLES: tuple[str, ...] = ()
REQUIRED_TABLES: tuple[str, ...] = ()
BLOCK = "encounter_block"


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


def metric_values(decisions: dict, measure: str) -> dict:
    return {d["id"]: d["value"] for d in decisions["metrics"][measure]["decisions"]}


def site_statistics(con, cfg: SiteConfig) -> dict:
    """When the site's ICU ADT rows begin and end, and the span of admissions."""
    location = shared_values(cfg.decisions())["icu_locations"].lower()
    first, last = con.execute(
        f"SELECT min(in_dttm), max(in_dttm) FROM read_parquet('{cfg.table_path('adt')}') "
        f"WHERE lower(CAST(location_category AS VARCHAR)) = '{location}'").fetchone()
    first_admission, last_admission = con.execute(
        f"SELECT min(admission_dttm), max(admission_dttm) FROM read_parquet('{cfg.table_path('hospitalization')}')"
    ).fetchone()
    return {"icu_records": {"first": utc_text(first), "last": utc_text(last),
                            "first_admission": utc_text(first_admission), "last_admission": utc_text(last_admission)}}


def data_quality(con, cfg: SiteConfig) -> dict:
    source = f"read_parquet('{cfg.table_path('adt')}')"
    return {"adt_rows_by_location_category": {str(k): int(n) for k, n in con.execute(
        f"SELECT lower(CAST(location_category AS VARCHAR)), count(*) FROM {source} GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()}}


def encounter_flow(adt: pd.DataFrame, icu_location: str, ed_location: str, readmit_hours: float) -> pd.DataFrame:
    """Per encounter with an ICU row: first ICU row, first transfer out of the ICU, return within
    `readmit_hours`, the ED stay before the first ICU row, and whether the last ADT row is an ICU.

    ADT rows are taken in order of in_dttm. A transfer out is an ICU row followed by a
    non-ICU row; its time is the ICU row's out_dttm, or the next row's in_dttm when out
    is missing. Only the first transfer out of each encounter counts, as on the
    respiratory dashboard.
    """
    adt = adt.sort_values([BLOCK, "in_dttm", "out_dttm"], kind="stable").reset_index(drop=True)
    category = lower(adt["location_category"])
    adt["is_icu"] = category.eq(icu_location.lower()).fillna(False).astype(bool)
    adt["is_ed"] = category.eq(ed_location.lower()).fillna(False).astype(bool)
    adt["order"] = adt.groupby(BLOCK).cumcount()
    grouped = adt.groupby(BLOCK)
    adt["next_is_icu"] = grouped["is_icu"].shift(-1)
    adt["next_in"] = grouped["in_dttm"].shift(-1)
    adt["previous_is_ed"] = grouped["is_ed"].shift(1).eq(True)
    # The start of each run of consecutive ED rows
    run = (adt["is_ed"] != grouped["is_ed"].shift(1)).groupby(adt[BLOCK]).cumsum()
    adt["run_start"] = adt.groupby([BLOCK, run])["in_dttm"].transform("min")
    adt["previous_run_start"] = grouped["run_start"].shift(1)

    icu = adt[adt["is_icu"]]
    first_icu = icu.drop_duplicates(BLOCK).set_index(BLOCK)
    flow = pd.DataFrame({
        "icu_in": first_icu["in_dttm"],
        "hospital": first_icu["hospital_id"].astype(str),
        "unit": first_icu["location_type"].map(prep.unit_label),
        "ed_start": first_icu["previous_run_start"].where(first_icu["previous_is_ed"]),
    })
    flow["last_is_icu"] = adt.drop_duplicates(BLOCK, keep="last").set_index(BLOCK)["is_icu"].reindex(flow.index)

    transfers = adt[adt["is_icu"] & adt["next_is_icu"].eq(False)].drop_duplicates(BLOCK)
    transfers = transfers.assign(t_out=transfers["out_dttm"].fillna(transfers["next_in"]))
    later = icu[[BLOCK, "order", "in_dttm"]].merge(transfers[[BLOCK, "order", "t_out"]], on=BLOCK,
                                                   suffixes=("", "_transfer"))
    later = later[later["order"] > later["order_transfer"]]
    first_return = later.groupby(BLOCK)["in_dttm"].min()
    transfers = transfers.set_index(BLOCK)
    flow["transfer_out"] = transfers["t_out"].reindex(flow.index)
    returned = first_return.reindex(transfers.index)
    flow["readmitted"] = (returned <= transfers["t_out"] + pd.Timedelta(hours=readmit_hours)).reindex(flow.index)
    return flow.reset_index()


def night_hours(times: pd.Series, tz: str, start_hour: float, end_hour: float) -> pd.Series:
    """True for local times from start_hour to midnight and from midnight to end_hour."""
    local = times.dt.tz_convert(tz)
    hour = local.dt.hour + local.dt.minute / 60
    return (hour >= start_hour) | (hour < end_hour)


def build(site: SiteBuild) -> BuildResult:
    cfg = site.cfg
    tz = cfg.site_timezone
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    readmit = metric_values(decisions, "icu_readmit_48h")
    night = metric_values(decisions, "night_transfer_out")
    death = {c.lower() for c in metric_values(decisions, "died_in_icu")["death"]}
    encounters = site.encounters
    adults = encounters[encounters["adult"]]

    intervals, unended = icu_intervals(site, shared["icu_locations"])
    icu_days = (intervals["out_dttm"] - intervals["in_dttm"]).dt.total_seconds().groupby(intervals[BLOCK]).sum() / 86400
    adt = site.adt[site.adt[BLOCK].isin(adults[BLOCK])]
    flow = encounter_flow(adt, shared["icu_locations"], shared["ed_locations"], readmit["readmit_hours"])
    icu_encounters = int(len(flow))

    flow["day"] = flow["icu_in"].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    flow = flow[in_window(flow["day"], cfg)]
    records = site.site_statistics.get("icu_records", {})
    coverage = whole_months(records.get("first"), records.get("last"), records.get("first_admission"),
                            records.get("last_admission"), tz) if shared["icu_coverage"] == "whole_months" else None
    if coverage is not None:
        before = len(flow)
        flow = flow[(flow["day"] >= coverage[0]) & (flow["day"] <= coverage[1])]
        log(f"ICU records and admissions cover whole months {coverage[0]:%Y-%m} to {coverage[1]:%Y-%m}; "
            f"{before - len(flow):,} ICU encounters admitted to the ICU outside them left out")
    flow = flow.reset_index(drop=True)

    encounter_index = encounters.set_index(BLOCK)
    blocks = flow[BLOCK]
    died = lower(blocks.map(encounter_index["discharge_category"])).isin(death).fillna(False).astype(bool)
    last_is_icu = flow["last_is_icu"].eq(True)
    transferred = flow["transfer_out"].notna()
    ed_hours = (flow["icu_in"] - flow["ed_start"]).dt.total_seconds() / 3600
    rows = pd.DataFrame({
        "row_id": blocks.map(encounter_index["hospitalization_id"]).astype(str),
        BLOCK: blocks,
        "label_id": blocks.map(encounter_index["hospitalization_id"]),
        "month": flow["day"].dt.strftime("%Y-%m"),
        "hospital": flow["hospital"],
        "unit": flow["unit"],
        "sex": blocks.map(encounter_index["sex_category"]).fillna("Unknown"),
        "race_eth": [prep.race_eth(encounter_index.at[b, "race_category"], encounter_index.at[b, "ethnicity_category"])
                     for b in blocks],
        "age_band": blocks.map(encounter_index["age"]).map(prep.age_band),
        "icu_admission": flow["icu_in"],
        "icu_days": blocks.map(icu_days).round(2),
        "transferred_out": transferred.astype(int),
        "readmitted_48h": (transferred & flow["readmitted"].eq(True)).astype(int),
        "night_transfer_out": (transferred & night_hours(flow["transfer_out"], tz, night["start_hour"],
                                                         night["end_hour"])).astype(int),
        "from_ed": flow["ed_start"].notna().astype(int),
        "ed_to_icu_hours": ed_hours.where(ed_hours >= 0).round(2),
        "died_in_icu": (died & last_is_icu).astype(int),
        "discharged_from_icu": (~died & last_is_icu).astype(int),
    })

    funnel = [
        {"step": "Encounters after linking", "n": int(len(encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with ICU time", "n": icu_encounters},
        {"step": "Admitted to the ICU in whole months of ICU records" if coverage is not None
         else "Admitted to the ICU (record coverage not applied)", "n": int(len(rows)), "unit": "encounters"},
        {"step": "Transferred out of the ICU alive", "n": int(rows["transferred_out"].sum())},
    ]
    log(f"{len(rows):,} ICU encounters; {int(rows['transferred_out'].sum()):,} transferred out of the ICU")
    return BuildResult(
        tables={"encounters": rows},
        summary={"funnel": funnel},
        meta={"months": sorted(rows["month"].unique().tolist())},
        data_quality={"icu_rows_without_end": unended,
                      "icu_encounters_with_negative_ed_time": int((ed_hours < 0).sum()),
                      "months_counted": None if coverage is None else
                      {"first": f"{coverage[0]:%Y-%m}", "last": f"{coverage[1]:%Y-%m}",
                       "icu_encounters_left_out": icu_encounters - int(len(rows))}},
    )
