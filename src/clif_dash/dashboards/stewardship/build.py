"""Stewardship build: days present and days of therapy by ICU, month and encounter.

Follows the NHSN Antimicrobial Use Option; every choice is in
config/decisions/stewardship.yaml. The build has one chunked phase:

1. Days present come from the site's ICU ADT rows: every local calendar day a
   row touches, per encounter and unit. ADT is small, so this runs in the main
   process.
2. medication_admin_intermittent is staged in chunks, keeping only the agents
   some measure counts. Each worker keeps administered doses by a counted
   route, places each in the ICU row that contains it, and counts one day of
   therapy per agent per local day per unit, for every agent group.
3. Days and days of therapy are summed to one row per encounter, hospital,
   ICU unit and month, so each day is attributed to its own month.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ... import stage
from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, assign_chunks, log
from ...upstream import prep

TABLES = ("medication_admin_intermittent",)
REQUIRED_TABLES = ("medication_admin_intermittent",)
# Measures counted as days of therapy, each a column dot_<id>; their agents are in the decisions
GROUPS = ("all_antibacterial", "broad_hospital_onset", "broad_community", "resistant_gram_positive",
          "narrow_beta_lactam", "cdi_risk", "antifungal_candidiasis")
DAY_KEY = ["encounter_block", "hospital", "unit", "day"]
ONE_MICROSECOND = pd.Timedelta(microseconds=1)


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


def group_agents(decisions: dict) -> dict[str, tuple[set[str], set[str]]]:
    """Each group's agents and the subset that counts only when given IV, lower-cased."""
    groups = {}
    for group in GROUPS:
        values = {d["id"]: d["value"] for d in decisions["metrics"][group]["decisions"]}
        groups[group] = ({a.lower() for a in values["agents"]}, {a.lower() for a in values.get("iv_only") or []})
    return groups


def lower(series: pd.Series) -> pd.Series:
    return series.astype("string").str.lower()


def icu_intervals(site: SiteBuild) -> tuple[pd.DataFrame, int]:
    """Adult encounters' ICU ADT rows with an end time; also how many rows had none at all."""
    cfg = site.cfg
    shared = shared_values(cfg.decisions())
    adults = site.encounters.loc[site.encounters["adult"], ["encounter_block", "discharge_dttm"]]
    adt = site.adt[lower(site.adt["location_category"]).eq(str(shared["icu_locations"]).lower()).fillna(False)]
    adt = adt.merge(adults, on="encounter_block")
    adt = adt.assign(out_dttm=adt["out_dttm"].fillna(adt["discharge_dttm"]))
    unended = int(adt["out_dttm"].isna().sum())
    adt = adt[adt["out_dttm"].notna() & (adt["out_dttm"] > adt["in_dttm"])]
    adt = adt.reset_index(drop=True)
    intervals = pd.DataFrame({
        "encounter_block": adt["encounter_block"],
        "hospital": adt["hospital_id"].astype(str),
        "unit": adt["location_type"].map(prep.unit_label),
        "in_dttm": adt["in_dttm"],
        "out_dttm": adt["out_dttm"],
    })
    return intervals.sort_values(["encounter_block", "in_dttm"], kind="stable").reset_index(drop=True), unended


def days_present(intervals: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Every local calendar day each ICU row touches, once per encounter, hospital and unit."""
    if intervals.empty:
        return pd.DataFrame(columns=DAY_KEY)
    first = intervals["in_dttm"].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    last = (intervals["out_dttm"] - ONE_MICROSECOND).dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    n_days = ((last - first).dt.days + 1).to_numpy()
    expanded = intervals.loc[intervals.index.repeat(n_days), ["encounter_block", "hospital", "unit"]]
    offsets = np.concatenate([np.arange(n) for n in n_days])
    expanded["day"] = first.repeat(n_days).to_numpy() + pd.to_timedelta(offsets, unit="D")
    return expanded.drop_duplicates(DAY_KEY).reset_index(drop=True)


def in_window(days: pd.Series, cfg: SiteConfig) -> pd.Series:
    keep = pd.Series(True, index=days.index)
    if cfg.start_date:
        keep &= days >= pd.Timestamp(cfg.start_date)
    if cfg.end_date:
        keep &= days <= pd.Timestamp(cfg.end_date)
    return keep


# --------------------------------------------------------------------------- #
# Whole-table counts (DuckDB over the full source table)
# --------------------------------------------------------------------------- #
def site_statistics(con, cfg: SiteConfig) -> dict:
    """When the site's records begin and end, over whole tables: the first and last
    administered dose of a counted agent by a counted route, and the first and last
    hospital admission."""
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    groups = group_agents(decisions)
    agents = ", ".join(f"'{a}'" for a in sorted(set().union(*(a for a, _ in groups.values()))))
    actions = ", ".join(f"'{a.lower()}'" for a in shared["administered_actions"])
    routes = ", ".join(f"'{r.lower()}'" for r in shared["routes"])
    first, last = con.execute(f"""
        SELECT min(admin_dttm), max(admin_dttm)
        FROM read_parquet('{cfg.table_path("medication_admin_intermittent")}')
        WHERE lower(CAST(med_category AS VARCHAR)) IN ({agents})
          AND lower(CAST(mar_action_group AS VARCHAR)) IN ({actions})
          AND lower(CAST(med_route_category AS VARCHAR)) IN ({routes})
    """).fetchone()
    first_admission, last_admission = con.execute(
        f"SELECT min(admission_dttm), max(admission_dttm) FROM read_parquet('{cfg.table_path('hospitalization')}')"
    ).fetchone()
    return {"medication_records": {"first_dose": utc_text(first), "last_dose": utc_text(last),
                                   "first_admission": utc_text(first_admission),
                                   "last_admission": utc_text(last_admission)}}


def utc_text(value) -> str | None:
    """A DuckDB timestamp as UTC text, so it pickles to workers and saves to summary.json."""
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    return str(stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC"))


def covered_months(statistics: dict, tz: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """The first and last local days of the whole months inside both the medication
    records and the span of hospital admissions.

    Before the first recorded dose an ICU day cannot show therapy. After the last
    admission, the ICU holds only patients admitted earlier, who stay longest and are
    the most treated, so use reads falsely high; before the first admission the data
    lack patients already in hospital. The edge months are only partly recorded. None
    when there are no recorded doses.
    """
    records = statistics.get("medication_records", {})
    if not records.get("first_dose"):
        return None
    local = lambda text: pd.Timestamp(text).tz_convert(tz).tz_localize(None).normalize()
    first = max(local(records["first_dose"]), local(records["first_admission"]))
    last = min(local(records["last_dose"]), local(records["last_admission"]))
    start = first if first.day == 1 else first + pd.offsets.MonthBegin(1)
    end = last if last.is_month_end else last.replace(day=1) - pd.Timedelta(days=1)
    return start, end


def data_quality(con, cfg: SiteConfig) -> dict:
    source = f"read_parquet('{cfg.table_path('medication_admin_intermittent')}')"
    columns = set(stage.source_columns(cfg, "medication_admin_intermittent"))
    groups = group_agents(cfg.decisions())
    counted = sorted(set().union(*(agents for agents, _ in groups.values())))
    agent_list = ", ".join(f"'{a}'" for a in counted)

    def scalar(sql):
        return int(con.execute(sql).fetchone()[0])

    routes = con.execute(
        f"SELECT lower(CAST(med_route_category AS VARCHAR)), count(*) FROM {source} "
        f"WHERE lower(CAST(med_category AS VARCHAR)) IN ({agent_list}) GROUP BY 1 ORDER BY 2 DESC").fetchall()
    uncounted = con.execute(
        f"SELECT lower(CAST(med_category AS VARCHAR)), count(*) FROM {source} "
        "WHERE lower(CAST(med_group AS VARCHAR)) = 'cms_sepsis_qualifying_antibiotics' "
        f"AND lower(CAST(med_category AS VARCHAR)) NOT IN ({agent_list}) GROUP BY 1 ORDER BY 2 DESC").fetchall() \
        if "med_group" in columns else []
    return {
        "antimicrobial_rows_without_time": scalar(
            f"SELECT count(*) FROM {source} WHERE admin_dttm IS NULL "
            f"AND lower(CAST(med_category AS VARCHAR)) IN ({agent_list})"),
        "antimicrobial_rows_by_route": {str(route): int(n) for route, n in routes},
        "unmapped_mar_action_rows": scalar(
            f"SELECT count(*) FROM {source} WHERE lower(CAST(mar_action_group AS VARCHAR)) = 'other'")
        if "mar_action_group" in columns else 0,
        "sepsis_antibiotic_categories_not_counted": {str(category): int(n) for category, n in uncounted},
    }


# --------------------------------------------------------------------------- #
# Days of therapy (per chunk)
# --------------------------------------------------------------------------- #
def therapy_chunk(cfg: SiteConfig, chunk: int, statistics: dict) -> dict:
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    groups = group_agents(decisions)
    work = cfg.stage_dir
    meds = stage.load_staged_chunk(cfg, "therapy", "medication_admin_intermittent", chunk)
    counts = {"staged": int(len(meds))}
    meds = meds[meds["admin_dttm"].notna()]
    meds = meds[lower(meds["mar_action_group"]).isin([a.lower() for a in shared["administered_actions"]]).fillna(False)]
    counts["administered"] = int(len(meds))
    meds = meds[lower(meds["med_route_category"]).isin([r.lower() for r in shared["routes"]]).fillna(False)]
    counts["counted_route"] = int(len(meds))

    mapping = pd.read_parquet(work / "mapping.parquet")
    intervals = pd.read_parquet(work / "icu_intervals.parquet")
    meds = meds.reset_index(drop=True)
    doses = pd.DataFrame({
        "hospitalization_id": meds["hospitalization_id"],
        "agent": lower(meds["med_category"]),
        "iv": lower(meds["med_route_category"]).eq("iv").fillna(False).astype(bool),
        "admin_dttm": meds["admin_dttm"],
    }).merge(mapping, on="hospitalization_id")
    doses["dose"] = np.arange(len(doses))
    # The ICU row that contains each dose: in <= time < out. Overlapping rows keep the earlier one.
    placed = doses.merge(intervals, on="encounter_block")
    placed = placed[(placed["in_dttm"] <= placed["admin_dttm"]) & (placed["admin_dttm"] < placed["out_dttm"])]
    placed = placed.sort_values(["dose", "in_dttm"], kind="stable").drop_duplicates("dose")
    counts["in_icu"] = int(len(placed))
    placed["day"] = placed["admin_dttm"].dt.tz_convert(cfg.site_timezone).dt.normalize().dt.tz_localize(None)
    placed = placed[in_window(placed["day"], cfg)]

    per_day = []
    for group, (agents, iv_only) in groups.items():
        counted = placed["agent"].isin(agents) & (~placed["agent"].isin(iv_only) | placed["iv"])
        dot = placed[counted].drop_duplicates(DAY_KEY + ["agent"]).groupby(DAY_KEY).size().rename(f"dot_{group}")
        per_day.append(dot)
    therapy = pd.concat(per_day, axis=1).fillna(0).astype(int).reset_index() if len(placed) else \
        pd.DataFrame(columns=DAY_KEY + [f"dot_{g}" for g in GROUPS])
    return {"chunk": chunk, "therapy": therapy, "counts": counts}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def build(site: SiteBuild) -> BuildResult:
    cfg = site.cfg
    work = cfg.stage_dir
    tz = cfg.site_timezone
    groups = group_agents(cfg.decisions())
    encounters = site.encounters
    adults = encounters[encounters["adult"]]

    intervals, unended = icu_intervals(site)
    intervals.to_parquet(work / "icu_intervals.parquet", index=False)
    days = days_present(intervals, tz)
    days = days[in_window(days["day"], cfg)].reset_index(drop=True)
    icu_encounters, icu_days = int(days["encounter_block"].nunique()), int(len(days))
    coverage = covered_months(site.site_statistics, tz) \
        if shared_values(cfg.decisions())["medication_coverage"] == "whole_months" else None
    if coverage is not None:
        days = days[(days["day"] >= coverage[0]) & (days["day"] <= coverage[1])].reset_index(drop=True)
        log(f"Medication records and admissions cover whole months {coverage[0]:%Y-%m} to {coverage[1]:%Y-%m}; "
            f"{icu_days - len(days):,} ICU days outside them left out")
    log(f"Days present: {len(days):,} in {days['encounter_block'].nunique():,} adult encounters with ICU time")

    icu_blocks = days["encounter_block"].unique()
    chunks = assign_chunks(icu_blocks, cfg.encounters_per_chunk, cfg.workers)
    agent_list = ", ".join(f"'{a}'" for a in sorted(set().union(*(agents for agents, _ in groups.values()))))
    staged = site.stage("medication_admin_intermittent", site.mapping.merge(chunks, on="encounter_block"), "therapy",
                        where=f"lower(CAST(source.med_category AS VARCHAR)) IN ({agent_list})")
    log(f"Therapy: {staged:,} antimicrobial administrations staged in {chunks['chunk'].nunique()} chunks")
    results = site.run_chunks(therapy_chunk, sorted(chunks["chunk"].unique()))
    therapy_parts = [r["therapy"] for r in results if len(r["therapy"])]
    dot_columns = [f"dot_{g}" for g in GROUPS]
    therapy = pd.concat(therapy_parts, ignore_index=True) if therapy_parts else pd.DataFrame(columns=DAY_KEY + dot_columns)
    counts = {k: sum(r["counts"][k] for r in results) for k in ("staged", "administered", "counted_route", "in_icu")} \
        if results else {"staged": 0, "administered": 0, "counted_route": 0, "in_icu": 0}

    days = days.merge(therapy, on=DAY_KEY, how="left")
    days[dot_columns] = days[dot_columns].fillna(0).astype(int)
    days["any_antibacterial"] = (days["dot_all_antibacterial"] > 0).astype(int)
    days["month"] = days["day"].dt.strftime("%Y-%m")
    rows = days.groupby(["encounter_block", "hospital", "unit", "month"], sort=True).agg(
        first_day=("day", "min"), last_day=("day", "max"), days_present=("day", "size"),
        days_any=("any_antibacterial", "sum"), **{c: (c, "sum") for c in dot_columns}).reset_index()

    encounter_index = encounters.set_index("encounter_block")
    blocks = rows["encounter_block"]
    rows["label_id"] = blocks.map(encounter_index["hospitalization_id"])
    rows["sex"] = blocks.map(encounter_index["sex_category"]).fillna("Unknown")
    rows["race_eth"] = [prep.race_eth(encounter_index.at[b, "race_category"],
                                      encounter_index.at[b, "ethnicity_category"]) for b in blocks]
    rows["age_band"] = blocks.map(encounter_index["age"]).map(prep.age_band)
    # The same ICU type at two hospitals in one month is two rows, so the hospital belongs in the id
    rows["row_id"] = rows["label_id"] + " · " + rows["hospital"] + " · " + rows["unit"] + " · " + rows["month"]
    for column in ("first_day", "last_day"):
        rows[column] = rows[column].dt.strftime("%Y-%m-%d")
    rows = rows[["row_id", "encounter_block", "label_id", "month", "hospital", "unit", "sex", "race_eth", "age_band",
                 "first_day", "last_day", "days_present", "days_any", *dot_columns]]

    funnel = [
        {"step": "Encounters after linking", "n": int(len(encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with ICU time", "n": icu_encounters},
        {"step": "With ICU time in whole months of medication records" if coverage is not None
         else "With ICU time (medication-record coverage not applied)", "n": int(rows["encounter_block"].nunique())},
        {"step": "ICU stay-months (encounter, unit, month)", "n": int(len(rows)), "unit": "rows"},
        {"step": "Days present", "n": int(rows["days_present"].sum()), "unit": "days"},
    ]
    log(f"{len(rows):,} stay-months; {int(rows['dot_all_antibacterial'].sum()):,} antibacterial days of therapy")
    return BuildResult(
        tables={"stay_months": rows},
        summary={"funnel": funnel},
        meta={"months": sorted(rows["month"].unique().tolist())},
        data_quality={"icu_rows_without_end": unended, "antimicrobial_administrations": counts,
                      "months_counted": None if coverage is None else
                      {"first": f"{coverage[0]:%Y-%m}", "last": f"{coverage[1]:%Y-%m}",
                       "icu_days_left_out": icu_days - int(rows["days_present"].sum())}},
    )
