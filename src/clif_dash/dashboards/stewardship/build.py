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
from ...icu import DAY_KEY, days_present, icu_intervals, in_window, lower, place_in_icu, utc_text, whole_months
from ...upstream import prep

TABLES = ("medication_admin_intermittent",)
REQUIRED_TABLES = ("medication_admin_intermittent",)
# Measures counted as days of therapy, each a column dot_<id>; their agents are in the decisions
GROUPS = ("all_antibacterial", "broad_hospital_onset", "broad_community", "resistant_gram_positive",
          "narrow_beta_lactam", "cdi_risk", "antifungal_candidiasis", "anaerobic")


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


def group_agents(decisions: dict) -> dict[str, tuple[set[str], set[str]]]:
    """Each group's agents and the subset that counts only when given IV, lower-cased."""
    groups = {}
    for group in GROUPS:
        values = {d["id"]: d["value"] for d in decisions["metrics"][group]["decisions"]}
        groups[group] = ({a.lower() for a in values["agents"]}, {a.lower() for a in values.get("iv_only") or []})
    return groups


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


def covered_months(statistics: dict, tz: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """The whole months inside both the medication records and the span of admissions (see icu.whole_months)."""
    records = statistics.get("medication_records", {})
    return whole_months(records.get("first_dose"), records.get("last_dose"),
                        records.get("first_admission"), records.get("last_admission"), tz)


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
        "route": lower(meds["med_route_category"]).fillna(""),
        "iv": lower(meds["med_route_category"]).eq("iv").fillna(False).astype(bool),
        "admin_dttm": meds["admin_dttm"],
    }).merge(mapping, on="hospitalization_id")
    placed = place_in_icu(doses, intervals, "admin_dttm", cfg.site_timezone)
    counts["in_icu"] = int(len(placed))
    placed = placed[in_window(placed["day"], cfg)]

    per_day = []
    for group, (agents, iv_only) in groups.items():
        counted = placed["agent"].isin(agents) & (~placed["agent"].isin(iv_only) | placed["iv"])
        dot = placed[counted].drop_duplicates(DAY_KEY + ["agent"]).groupby(DAY_KEY).size().rename(f"dot_{group}")
        per_day.append(dot)
    per_day.append(redundant_anaerobic_days(placed, groups["anaerobic"][0], decisions))
    therapy = pd.concat(per_day, axis=1).fillna(0).astype(int).reset_index() if len(placed) else \
        pd.DataFrame(columns=DAY_KEY + [f"dot_{g}" for g in GROUPS] + ["redundant_anaerobic"])
    return {"chunk": chunk, "therapy": therapy, "counts": counts}


def redundant_anaerobic_days(placed: pd.DataFrame, anaerobic: set[str], decisions: dict) -> pd.Series:
    """1 for each ICU day with two or more anti-anaerobic agents, when the unit's previous or next
    day also had two or more; a switch from one agent to another on a single day does not count.

    Agent-route pairs listed under excluded_from_count (oral metronidazole, which treats
    C. difficile) do not count toward the two.
    """
    values = {d["id"]: d["value"] for d in decisions["metrics"]["redundant_anaerobic"]["decisions"]}
    excluded = {tuple(pair.lower().split(":")) for pair in values["excluded_from_count"]}
    kept = [(agent, route) not in excluded for agent, route in zip(placed["agent"], placed["route"])]
    counted = placed[placed["agent"].isin(anaerobic) & np.array(kept, dtype=bool)]
    agents = counted.drop_duplicates(DAY_KEY + ["agent"]).groupby(DAY_KEY).size().rename("agents").reset_index()
    double = agents[agents["agents"] >= 2][DAY_KEY]
    stay = ["encounter_block", "hospital", "unit"]
    neighbours = pd.concat([double.assign(day=double["day"] + pd.Timedelta(days=shift)) for shift in (-1, 1)])
    confirmed = double.merge(neighbours.drop_duplicates(), on=stay + ["day"])
    return confirmed.drop_duplicates(DAY_KEY).set_index(DAY_KEY).assign(redundant_anaerobic=1)["redundant_anaerobic"]


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

    intervals, unended = icu_intervals(site, shared_values(cfg.decisions())["icu_locations"])
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
    therapy_columns = dot_columns + ["redundant_anaerobic"]
    days = days.reindex(columns=[*days.columns, *[c for c in therapy_columns if c not in days.columns]])
    days[therapy_columns] = days[therapy_columns].fillna(0).astype(int)
    days["any_antibacterial"] = (days["dot_all_antibacterial"] > 0).astype(int)
    days["any_anaerobic"] = (days["dot_anaerobic"] > 0).astype(int)
    days["month"] = days["day"].dt.strftime("%Y-%m")
    rows = days.groupby(["encounter_block", "hospital", "unit", "month"], sort=True).agg(
        first_day=("day", "min"), last_day=("day", "max"), days_present=("day", "size"),
        days_any=("any_antibacterial", "sum"), days_anaerobic=("any_anaerobic", "sum"),
        days_redundant_anaerobic=("redundant_anaerobic", "sum"), **{c: (c, "sum") for c in dot_columns}).reset_index()

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
                 "first_day", "last_day", "days_present", "days_any", "days_anaerobic", "days_redundant_anaerobic",
                 *dot_columns]]

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
