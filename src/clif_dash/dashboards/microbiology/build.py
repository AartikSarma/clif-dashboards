"""Microbiology build: cultures, hospital-onset infections and resistance, by ICU and month.

Every choice is in config/decisions/microbiology.yaml. Culture and C. difficile
tables are small (rows per hospitalization, not per hour), so DuckDB reads only the
rows for adult encounters with ICU time and the work runs in the main process:

1. Days present, as on the stewardship dashboard (src/clif_dash/icu.py).
2. Blood and urine culture draws: one per encounter, specimen type and collection
   time. Each blood draw is classified as contaminated (only unmatched common
   commensals) or positive (a pathogen, or a matched commensal).
3. Hospital-onset bacteremia: positive draws in the ICU on hospital day 4 or later,
   one per 14 days per encounter.
4. Hospital-onset C. difficile: NHSN LabID events (14-day duplicates removed),
   hospital-onset and incident, in the ICU.
5. New IV antibacterial starts in the ICU, found in SQL over the medication table,
   and whether a blood culture preceded each.
6. First isolates per encounter and organism from cultures collected in the ICU,
   with methicillin, ceftriaxone and carbapenem results.
7. Respiratory virus tests (microbiology_nonculture): one specimen per encounter and
   collection time, placed in the ICU stay that contains it or begins within 48 h
   after it, with tested and detected flags per virus group; each encounter's first
   positive per group is a case on the stay-month rows.

Rows are one encounter's days in one ICU at one hospital in one month, as for
stewardship; antibiotic starts and isolates are secondary tables linked by row_id.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, log
from ...icu import DAY_KEY, days_present, icu_intervals, in_window, lower, place_in_icu, utc_text, whole_months
from ...stage import clif_conventions
from ...upstream import prep

TABLES = ("microbiology_culture", "microbiology_nonculture", "microbiology_susceptibility",
          "medication_admin_intermittent")
REQUIRED_TABLES = ("microbiology_culture",)
STAY = ["encounter_block", "hospital", "unit", "month"]
# Respiratory virus groups, each with a cases and a positivity measure; their organisms are a decision
RESPIRATORY_GROUPS = ("sars_cov2", "influenza", "rsv", "other_respiratory_virus")


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


def measure_values(decisions: dict, measure: str) -> dict:
    return {d["id"]: d["value"] for d in decisions["metrics"][measure]["decisions"]}


def sql_list(values) -> str:
    return ", ".join("'" + str(v).lower().replace("'", "''") + "'" for v in values)


def matches_any(categories: pd.Series, entries: list[str]) -> pd.Series:
    """Categories equal to an entry, or starting with an entry that ends in "_" (a genus)."""
    exact = {e for e in entries if not e.endswith("_")}
    prefixes = tuple(e for e in entries if e.endswith("_"))
    values = categories.fillna("")
    return values.isin(exact) | (values.str.startswith(prefixes) if prefixes else False)


# --------------------------------------------------------------------------- #
# Whole-table statistics and counts
# --------------------------------------------------------------------------- #
def site_statistics(con, cfg: SiteConfig) -> dict:
    """When the site's culture records begin and end, and the span of admissions."""
    first, last = con.execute(
        f"SELECT min(collect_dttm), max(collect_dttm) FROM read_parquet('{cfg.table_path('microbiology_culture')}')"
    ).fetchone()
    first_admission, last_admission = con.execute(
        f"SELECT min(admission_dttm), max(admission_dttm) FROM read_parquet('{cfg.table_path('hospitalization')}')"
    ).fetchone()
    return {"culture_records": {"first": utc_text(first), "last": utc_text(last),
                                "first_admission": utc_text(first_admission),
                                "last_admission": utc_text(last_admission)}}


def data_quality(con, cfg: SiteConfig) -> dict:
    culture = f"read_parquet('{cfg.table_path('microbiology_culture')}')"

    def scalar(sql):
        return int(con.execute(sql).fetchone()[0])

    quality = {
        "culture_rows_without_collection_time": scalar(f"SELECT count(*) FROM {culture} WHERE collect_dttm IS NULL"),
        "culture_rows_by_method": {str(k): int(n) for k, n in con.execute(
            f"SELECT lower(CAST(method_category AS VARCHAR)), count(*) FROM {culture} GROUP BY 1 ORDER BY 2 DESC").fetchall()},
    }
    susceptibility = cfg.table_path("microbiology_susceptibility")
    if susceptibility.exists():
        quality["susceptibility_rows_by_result"] = {str(k): int(n) for k, n in con.execute(
            f"SELECT lower(CAST(susceptibility_category AS VARCHAR)), count(*) FROM read_parquet('{susceptibility}') "
            "GROUP BY 1 ORDER BY 2 DESC").fetchall()}
    quality["tables_present"] = {t: cfg.table_path(t).exists() for t in TABLES}
    return quality


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def read(con, sql: str) -> pd.DataFrame:
    return clif_conventions(con.execute(sql).df())


def cultures(con, cfg: SiteConfig) -> pd.DataFrame:
    """Culture rows (method culture) for the registered encounters, lower-cased, with encounter_block."""
    return read(con, f"""
        SELECT m.encounter_block, m.patient_id, c.organism_id, c.collect_dttm,
               lower(CAST(c.fluid_category AS VARCHAR)) AS fluid,
               lower(CAST(c.organism_category AS VARCHAR)) AS organism,
               lower(CAST(c.organism_group AS VARCHAR)) AS organism_group
        FROM read_parquet('{cfg.table_path("microbiology_culture")}') AS c
        JOIN encounter_map AS m ON CAST(c.hospitalization_id AS VARCHAR) = m.hospitalization_id
        WHERE lower(CAST(c.method_category AS VARCHAR)) = 'culture' AND c.collect_dttm IS NOT NULL
    """)


def draws(rows: pd.DataFrame, fluids: list[str]) -> pd.DataFrame:
    """One row per encounter and collection time among culture rows of these fluids."""
    kept = rows[rows["fluid"].isin([f.lower() for f in fluids])]
    return kept[["encounter_block", "collect_dttm"]].drop_duplicates().reset_index(drop=True)


def classify_blood_draws(rows: pd.DataFrame, shared: dict, tz: str) -> pd.DataFrame:
    """Each blood draw, with `contaminated` and `positive` flags.

    A common commensal is matched when the same organism category grows from another
    blood draw of the encounter (a different collection time) on the same or next
    local calendar day. A draw is contaminated when it grows only unmatched common
    commensals, and positive when it grows anything else or a matched commensal.
    """
    blood = rows[rows["fluid"].isin([f.lower() for f in shared["blood_fluids"]])].copy()
    blood["date"] = blood["collect_dttm"].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    growth = blood[blood["organism"].notna() & blood["organism"].ne("no_growth")].copy()
    commensal = (growth["organism_group"].isin(shared["commensal_groups"])
                 | matches_any(growth["organism"], shared["commensal_categories"]))
    growth["commensal"] = commensal & ~growth["organism"].isin(shared["commensal_exceptions"])
    pairs = growth[growth["commensal"]][["encounter_block", "collect_dttm", "date", "organism"]]
    other = pairs.rename(columns={"collect_dttm": "other_time", "date": "other_date"})
    joined = pairs.merge(other, on=["encounter_block", "organism"])
    joined = joined[(joined["other_time"] != joined["collect_dttm"])
                    & ((joined["other_date"] - joined["date"]).dt.days.abs() <= 1)]
    matched = joined[["encounter_block", "collect_dttm", "organism"]].drop_duplicates().assign(matched=True)
    growth = growth.merge(matched, on=["encounter_block", "collect_dttm", "organism"], how="left")
    growth["matched"] = growth["matched"].eq(True)
    growth["real"] = ~growth["commensal"] | growth["matched"]
    per_draw = growth.groupby(["encounter_block", "collect_dttm"]).agg(
        any_growth=("organism", "size"), any_real=("real", "any")).reset_index()
    result = draws(blood, shared["blood_fluids"]).merge(per_draw, on=["encounter_block", "collect_dttm"], how="left")
    result["positive"] = result["any_real"].eq(True)
    result["contaminated"] = (result["any_growth"].fillna(0) > 0) & ~result["positive"]
    return result[["encounter_block", "collect_dttm", "positive", "contaminated"]]


def hospital_day(events: pd.DataFrame, encounters: pd.DataFrame, tz: str) -> pd.Series:
    """Hospital day of each event's local `day`, where day 1 is the encounter's admission date."""
    admitted = encounters.set_index("encounter_block")["admission_dttm"]
    admission_day = events["encounter_block"].map(admitted).dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    return (events["day"] - admission_day).dt.days + 1


def first_after_gap(events: pd.DataFrame, key: list[str], gap_days: int) -> pd.DataFrame:
    """Events that start a new episode: the first, and each later one at least `gap_days`
    after the start of the previous episode (a fixed repeat window, as NHSN's RIT)."""
    kept = []
    for _, group in events.sort_values("collect_dttm").groupby(key, sort=False):
        last = None
        for index, day in zip(group.index, group["day"]):
            if last is None or (day - last).days >= gap_days:
                kept.append(index)
                last = day
    return events.loc[kept]


# --------------------------------------------------------------------------- #
# Hospital-onset C. difficile (NHSN LabID)
# --------------------------------------------------------------------------- #
def cdi_events(con, cfg: SiteConfig, intervals: pd.DataFrame, encounters: pd.DataFrame, values: dict) -> pd.DataFrame:
    """Incident, hospital-onset C. difficile LabID events placed in the ICU."""
    path = cfg.table_path("microbiology_nonculture")
    if not path.exists():
        return pd.DataFrame(columns=DAY_KEY)
    tests = read(con, f"""
        SELECT m.encounter_block, m.patient_id, n.collect_dttm
        FROM read_parquet('{path}') AS n
        JOIN encounter_map AS m ON CAST(n.hospitalization_id AS VARCHAR) = m.hospitalization_id
        WHERE lower(CAST(n.organism_category AS VARCHAR)) = 'clostridium_difficile'
          AND lower(CAST(n.result_category AS VARCHAR)) = 'detected' AND n.collect_dttm IS NOT NULL
    """)
    tests = tests.drop_duplicates(["encounter_block", "collect_dttm"])
    if tests.empty:
        return pd.DataFrame(columns=DAY_KEY)
    tz = cfg.site_timezone
    placed = place_in_icu(tests, intervals, "collect_dttm", tz)
    tests = tests.merge(placed[["encounter_block", "collect_dttm", "hospital", "unit", "day"]],
                        on=["encounter_block", "collect_dttm"], how="left")
    tests["location"] = tests["hospital"].fillna("") + "|" + tests["unit"].fillna("outside an ICU")
    tests["day"] = tests["collect_dttm"].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    tests = tests.sort_values("collect_dttm", kind="stable")
    # 14-day rule: a positive within 14 days of the patient's most recent positive in the location is a duplicate
    previous = tests.groupby(["patient_id", "location"])["day"].shift()
    events = tests[previous.isna() | ((tests["day"] - previous).dt.days > values["duplicate_days"])].copy()
    # Incident: no LabID event for the patient, in any location, in the previous 56 days
    prior = events.groupby("patient_id")["day"].shift()
    events = events[prior.isna() | ((events["day"] - prior).dt.days > values["incident_days"])]
    events = events[events["unit"].notna()]
    events = events[hospital_day(events, encounters, tz) >= values["onset_day"]]
    return events[DAY_KEY].assign(cdi_events=1)


# --------------------------------------------------------------------------- #
# Respiratory viruses
# --------------------------------------------------------------------------- #
def respiratory_specimens(con, cfg: SiteConfig, intervals: pd.DataFrame, shared: dict) -> pd.DataFrame:
    """One row per respiratory specimen (encounter and collection time) for an ICU stay, with
    tested_<group> and detected_<group> per virus group, and the stay's hospital, unit and day.

    A specimen belongs to the ICU row that contains its collection time, or else to the first
    ICU row beginning within `respiratory_window_hours` after it, placed on that row's first day.
    """
    columns = ["encounter_block", "collect_dttm", "hospital", "unit", "day",
               *[f"{k}_{g}" for g in RESPIRATORY_GROUPS for k in ("tested", "detected")]]
    path = cfg.table_path("microbiology_nonculture")
    if not path.exists():
        return pd.DataFrame(columns=columns)
    groups = {g: [o.lower() for o in shared["respiratory_pathogens"].get(g, [])] for g in RESPIRATORY_GROUPS}
    organisms = sql_list(sorted(set().union(*groups.values())))
    rows = read(con, f"""
        SELECT m.encounter_block, n.collect_dttm, lower(CAST(n.organism_category AS VARCHAR)) AS organism,
               lower(CAST(n.result_category AS VARCHAR)) = 'detected' AS detected
        FROM read_parquet('{path}') AS n
        JOIN encounter_map AS m ON CAST(n.hospitalization_id AS VARCHAR) = m.hospitalization_id
        WHERE n.collect_dttm IS NOT NULL AND lower(CAST(n.organism_category AS VARCHAR)) IN ({organisms})
          AND (n.fluid_category IS NULL
               OR lower(CAST(n.fluid_category AS VARCHAR)) IN ({sql_list(shared["respiratory_specimen"])}))
    """)
    if rows.empty:
        return pd.DataFrame(columns=columns)
    for group, members in groups.items():
        rows[f"tested_{group}"] = rows["organism"].isin(members)
        rows[f"detected_{group}"] = rows[f"tested_{group}"] & rows["detected"].fillna(False).astype(bool)
    flags = [c for c in columns if c.startswith(("tested_", "detected_"))]
    specimens = rows.groupby(["encounter_block", "collect_dttm"])[flags].any().astype(int).reset_index()
    window = pd.Timedelta(hours=shared["respiratory_window_hours"])
    candidates = specimens.merge(intervals, on="encounter_block")
    candidates = candidates[(candidates["in_dttm"] - window <= candidates["collect_dttm"])
                            & (candidates["collect_dttm"] < candidates["out_dttm"])]
    candidates = candidates.sort_values(["encounter_block", "collect_dttm", "in_dttm"], kind="stable") \
        .drop_duplicates(["encounter_block", "collect_dttm"])
    placed_at = candidates["collect_dttm"].where(candidates["collect_dttm"] >= candidates["in_dttm"],
                                                 candidates["in_dttm"])
    candidates["day"] = placed_at.dt.tz_convert(cfg.site_timezone).dt.normalize().dt.tz_localize(None)
    return candidates[columns].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# New antibacterial starts
# --------------------------------------------------------------------------- #
def antibacterial_starts(con, cfg: SiteConfig, values: dict) -> pd.DataFrame:
    """Administered IV antibacterial doses with no antibacterial dose in the encounter's previous `new_start` hours."""
    path = cfg.table_path("medication_admin_intermittent")
    if not path.exists():
        return pd.DataFrame(columns=["encounter_block", "start_dttm"])
    return read(con, f"""
        WITH doses AS (
            SELECT m.encounter_block, d.admin_dttm,
                   lower(CAST(d.med_route_category AS VARCHAR)) = 'iv' AS iv
            FROM read_parquet('{path}') AS d
            JOIN encounter_map AS m ON CAST(d.hospitalization_id AS VARCHAR) = m.hospitalization_id
            WHERE lower(CAST(d.med_category AS VARCHAR)) IN ({sql_list(values["agents"])})
              AND lower(CAST(d.mar_action_group AS VARCHAR)) = 'administered'
              AND d.admin_dttm IS NOT NULL
        ), ordered AS (
            SELECT *, lag(admin_dttm) OVER (PARTITION BY encounter_block ORDER BY admin_dttm) AS previous_dttm
            FROM doses
        )
        SELECT encounter_block, admin_dttm AS start_dttm FROM ordered
        WHERE iv AND (previous_dttm IS NULL OR admin_dttm - previous_dttm > INTERVAL {int(values["new_start"])} HOUR)
    """)


# --------------------------------------------------------------------------- #
# Isolates and resistance
# --------------------------------------------------------------------------- #
RESISTANCE = {"mrsa": "mrsa", "ceftriaxone_ns_enterobacterales": "ctx", "carbapenem_ns_gram_negatives": "carb"}


def isolates(con, cfg: SiteConfig, rows: pd.DataFrame, intervals: pd.DataFrame, decisions: dict) -> pd.DataFrame:
    """First isolate per encounter and organism category from cultures collected in the ICU,
    with a numerator and denominator column for each resistance measure."""
    tz = cfg.site_timezone
    growth = rows[rows["organism"].notna() & rows["organism"].ne("no_growth")]
    placed = place_in_icu(growth, intervals, "collect_dttm", tz)
    if placed.empty:
        return pd.DataFrame(columns=DAY_KEY + ["organism"])
    first = placed.sort_values("collect_dttm", kind="stable").drop_duplicates(["encounter_block", "organism"])
    path = cfg.table_path("microbiology_susceptibility")
    results = read(con, f"""
        SELECT CAST(organism_id AS VARCHAR) AS organism_id,
               lower(CAST(antimicrobial_category AS VARCHAR)) AS drug,
               lower(CAST(susceptibility_category AS VARCHAR)) AS result
        FROM read_parquet('{path}')
    """) if path.exists() else pd.DataFrame(columns=["organism_id", "drug", "result"])
    results = results[results["result"].isin(["susceptible", "non_susceptible"])]
    first = first.assign(organism_id=first["organism_id"].astype(str)).reset_index(drop=True)
    tested = first[["organism_id", "organism"]].merge(results, on="organism_id")
    for measure, prefix in RESISTANCE.items():
        values = measure_values(decisions, measure)
        ignored = {tuple(pair.split(":")) for pair in values.get("not_tested") or []}
        kept = np.array([(drug, organism) not in ignored for drug, organism in zip(tested["drug"], tested["organism"])],
                        dtype=bool)
        relevant = tested[matches_any(tested["organism"], values["organisms"]) & tested["drug"].isin(values["drugs"])
                          & kept]
        by_isolate = relevant.groupby("organism_id")["result"].agg(lambda r: (r == "non_susceptible").any())
        first[f"{prefix}_den"] = first["organism_id"].isin(by_isolate.index).astype(int)
        first[f"{prefix}_num"] = first["organism_id"].map(by_isolate).eq(True).astype(int)
    keep = first[[f"{p}_den" for p in RESISTANCE.values()]].sum(axis=1) > 0
    return first[keep][DAY_KEY + ["organism", "collect_dttm"] + [f"{p}_{s}" for p in RESISTANCE.values()
                                                                  for s in ("num", "den")]]


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
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
    records = site.site_statistics.get("culture_records", {})
    coverage = whole_months(records.get("first"), records.get("last"), records.get("first_admission"),
                            records.get("last_admission"), tz) if shared["culture_coverage"] == "whole_months" else None
    if coverage is not None:
        days = days[(days["day"] >= coverage[0]) & (days["day"] <= coverage[1])].reset_index(drop=True)
        log(f"Culture records and admissions cover whole months {coverage[0]:%Y-%m} to {coverage[1]:%Y-%m}; "
            f"{icu_days - len(days):,} ICU days outside them left out")
    counted_day = days[DAY_KEY].drop_duplicates()

    # Every encounter with ICU time: rules that look across the encounter need its non-ICU rows too
    icu_blocks = intervals["encounter_block"].unique()
    encounter_map = site.mapping[site.mapping["encounter_block"].isin(icu_blocks)].merge(
        encounters[["encounter_block", "patient_id"]], on="encounter_block")
    con.register("encounter_map", encounter_map)
    culture_rows = cultures(con, cfg)
    log(f"Microbiology: {len(culture_rows):,} culture rows for {len(icu_blocks):,} adult encounters with ICU time")

    def in_icu(events: pd.DataFrame, time_column: str) -> pd.DataFrame:
        placed = place_in_icu(events, intervals, time_column, tz)
        return placed.merge(counted_day, on=DAY_KEY)

    blood = in_icu(classify_blood_draws(culture_rows, shared, tz), "collect_dttm")
    urine = in_icu(draws(culture_rows, shared["urine_fluids"]), "collect_dttm")
    hob_values = measure_values(decisions, "hospital_onset_bacteremia")
    positive = blood[blood["positive"]].copy()
    positive = positive[hospital_day(positive, encounters, tz) >= hob_values["onset_day"]]
    hob = first_after_gap(positive, ["encounter_block"], hob_values["repeat_days"])
    cdi = cdi_events(con, cfg, intervals, encounters, measure_values(decisions, "hospital_onset_cdi")).merge(
        counted_day, on=DAY_KEY)

    start_values = measure_values(decisions, "cultures_before_antibiotics")
    starts = in_icu(antibacterial_starts(con, cfg, start_values), "start_dttm")
    all_blood = draws(culture_rows, shared["blood_fluids"]).rename(columns={"collect_dttm": "culture_dttm"})
    window = pd.Timedelta(hours=start_values["culture_window_h"])
    candidates = starts[["encounter_block", "start_dttm"]].merge(all_blood, on="encounter_block")
    cultured = candidates[(candidates["culture_dttm"] <= candidates["start_dttm"])
                          & (candidates["culture_dttm"] >= candidates["start_dttm"] - window)]
    starts["cultured"] = starts.set_index(["encounter_block", "start_dttm"]).index.isin(
        cultured.set_index(["encounter_block", "start_dttm"]).index).astype(int)
    found = isolates(con, cfg, culture_rows, intervals, decisions).merge(counted_day, on=DAY_KEY)
    specimens = respiratory_specimens(con, cfg, intervals, shared).merge(counted_day, on=DAY_KEY)
    con.unregister("encounter_map")
    # A case: each encounter's first positive specimen for a virus group
    cases = [specimens[specimens[f"detected_{g}"] == 1].sort_values("collect_dttm", kind="stable")
             .drop_duplicates("encounter_block").groupby(DAY_KEY).size().rename(f"{g}_cases")
             for g in RESPIRATORY_GROUPS]

    def per_day(events: pd.DataFrame, column: str, value: str | None = None) -> pd.Series:
        counted = events if value is None else events[events[value].astype(bool)]
        return counted.groupby(DAY_KEY).size().rename(column)

    daily = pd.concat([per_day(blood, "blood_draws"), per_day(blood, "contaminated_draws", "contaminated"),
                       per_day(urine, "urine_draws"), per_day(hob, "hob_events"),
                       cdi.groupby(DAY_KEY).size().rename("cdi_events"), *cases], axis=1).reset_index()
    counts = ["blood_draws", "contaminated_draws", "urine_draws", "hob_events", "cdi_events",
              *[f"{g}_cases" for g in RESPIRATORY_GROUPS]]
    days = days.merge(daily, on=DAY_KEY, how="left") if len(daily) else days.assign(**{c: 0 for c in counts})
    days[counts] = days[counts].fillna(0).astype(int)
    days["month"] = days["day"].dt.strftime("%Y-%m")
    rows = days.groupby(STAY, sort=True).agg(
        first_day=("day", "min"), last_day=("day", "max"), days_present=("day", "size"),
        **{c: (c, "sum") for c in counts}).reset_index()

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
                 "first_day", "last_day", "days_present", *counts]]

    def linked(events: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
        """Secondary rows carry their stay-month's row_id, month, unit and demographics."""
        events = events.assign(month=events["day"].dt.strftime("%Y-%m"))
        stay = rows[["row_id", *STAY, "label_id", "sex", "race_eth", "age_band"]]
        return events.merge(stay, on=STAY)[["row_id", "encounter_block", "label_id", "month", "hospital", "unit",
                                            "sex", "race_eth", "age_band", *columns]].reset_index(drop=True)

    start_rows = linked(starts.assign(start_time=starts["start_dttm"]), ["start_time", "cultured"])
    isolate_rows = linked(found.assign(collected=found["collect_dttm"]),
                          ["organism", "collected", *[c for c in found.columns if c.endswith(("_num", "_den"))]])
    specimen_rows = linked(specimens.assign(collected=specimens["collect_dttm"]),
                           ["collected", *[c for c in specimens.columns if c.startswith(("tested_", "detected_"))]])

    funnel = [
        {"step": "Encounters after linking", "n": int(len(encounters))},
        {"step": "Adults", "n": int(len(adults))},
        {"step": "Adults with ICU time", "n": icu_encounters},
        {"step": "With ICU time in whole months of culture records" if coverage is not None
         else "With ICU time (culture-record coverage not applied)", "n": int(rows["encounter_block"].nunique())},
        {"step": "ICU stay-months (encounter, unit, month)", "n": int(len(rows)), "unit": "rows"},
        {"step": "Days present", "n": int(rows["days_present"].sum()), "unit": "days"},
    ]
    log(f"{len(rows):,} stay-months; {int(rows['blood_draws'].sum()):,} blood culture draws; "
        f"{len(start_rows):,} antibacterial starts; {len(isolate_rows):,} first isolates; "
        f"{len(specimen_rows):,} respiratory virus specimens")
    return BuildResult(
        tables={"stay_months": rows, "antibiotic_starts": start_rows, "isolates": isolate_rows,
                "respiratory_specimens": specimen_rows},
        summary={"funnel": funnel},
        meta={"months": sorted(rows["month"].unique().tolist())},
        data_quality={"icu_rows_without_end": unended,
                      "months_counted": None if coverage is None else
                      {"first": f"{coverage[0]:%Y-%m}", "last": f"{coverage[1]:%Y-%m}",
                       "icu_days_left_out": icu_days - int(rows["days_present"].sum())}},
    )
