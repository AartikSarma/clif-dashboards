"""Respiratory build: IMV episodes, their quality measures, and proning rows.

The metric logic is upstream's (src/clif_dash/upstream, from
cag-lab/clif-icu-quality-dashboard at b3cdf29). The platform has already
stitched encounters; this module runs two chunked phases and the whole-cohort
steps:

1. Phase A. respiratory_support for adult encounters is staged in chunks; each
   worker runs the clifpy waterfall and finds IMV episodes for its chunk, then
   applies the cohort exclusions (ICU, outside-hospital intubation, date window).
2. Phase B. The large tables are staged only for encounters left in the cohort,
   in smaller chunks; each worker computes the per-episode metrics and the
   proning rows for its chunk.
3. Steps that need the whole cohort run last: the site-fitted mortality model
   and expected proning (BMI imputation uses the cohort median).

Every per-encounter computation sees exactly the rows it would have seen in the
single-process upstream build, which tests/test_oracle.py checks.
"""
from __future__ import annotations

import faulthandler
import multiprocessing
import os
import sys
import time
from importlib.metadata import PackageNotFoundError, version

import numpy as np
import pandas as pd

from ... import stage
from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, assign_chunks, log
from ...upstream import clif_io, metrics, prep, proning

# Tables the respiratory measures read, and the key that links each to an encounter
HOSPITALIZATION_KEYED = [
    "respiratory_support", "vitals", "labs", "position", "patient_assessments",
    "medication_admin_continuous", "hospital_diagnosis",
]
PATIENT_KEYED = ["code_status"]
TABLES = (*HOSPITALIZATION_KEYED, *PATIENT_KEYED)
REQUIRED_TABLES = ("respiratory_support",)

EPISODE_COLUMNS = [
    "episode_id", "encounter_block", "label_id", "start", "end", "month", "hospital", "unit", "sex", "race_eth",
    "age_band", "code_status", "trach", "n_runs", "age", "sofa", "cci", "pbw", "height_cm", "vt_hours_total",
    "ltvv_num", "ltvv_den", "vt24", "vt_pbw_median", "plat_num", "plat_den", "hdoc_num", "hdoc_den",
    "vent_days", "sat_num", "sat_doc", "sat_pheno", "sat_den", "para8_num", "para8_den", "para8_hours_max",
    "sbt_num", "sbt_doc", "sbt_pheno", "sbt_den",
    "vfd28", "imv_days", "ext_den", "reint_num", "rass_num", "rass_den", "rass_median", "deep_num", "deep_den",
    "died", "mort_expected", "icu_los", "hosp_los", "trach_any", "readmit_num", "readmit_den", "outcome_den",
    "discharge",
]
COMPLETENESS_FLAGS = ["has_abg", "has_height", "has_weight", "has_plateau", "has_rass", "has_vt"]


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


# The waterfall marks a row IMV only when it says so, or when it names no device and has an IMV mode
# (clifpy waterfall step 1-a); later steps only carry IMV forward from such rows. CLIF 3.0 names
# two of the modes acvc and pressure_control.
IMV_MODES = "assist control-volume control|acvc|simv|pressure control|pressure_control"


def ventilated_blocks(con, cfg: SiteConfig, mapping: pd.DataFrame) -> set:
    """Encounters with at least one respiratory_support row the waterfall could mark IMV.

    The waterfall works encounter by encounter, so leaving the others out changes no
    IMV episode, and it is most of the build's time (a Python call per mode block).
    """
    con.register("encounter_map", mapping[["hospitalization_id", "encounter_block"]])
    rows = con.execute(f"""
        SELECT DISTINCT m.encounter_block
        FROM read_parquet('{cfg.table_path("respiratory_support")}') AS r
        JOIN encounter_map AS m ON CAST(r.hospitalization_id AS VARCHAR) = m.hospitalization_id
        WHERE lower(CAST(r.device_category AS VARCHAR)) = 'imv'
           OR (r.device_category IS NULL AND r.device_name IS NULL
               AND regexp_matches(lower(CAST(r.mode_category AS VARCHAR)), '{IMV_MODES}'))
    """).fetchall()
    con.unregister("encounter_map")
    return {block for (block,) in rows}


def drop_untimed_rows(respiratory_support: pd.DataFrame) -> pd.DataFrame:
    """Drop respiratory_support rows with no recorded_dttm before the waterfall.

    clifpy's waterfall carries such rows through with a missing time, and later
    time-ordered joins (proning's ABG-to-ventilator match, timelines) then fail.
    A setting with no time cannot be placed on the timeline, so dropping it loses
    nothing; data_quality reports how many rows the site has.
    """
    return respiratory_support[respiratory_support["recorded_dttm"].notna()].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Whole-table statistics and counts (DuckDB over the full source tables)
# --------------------------------------------------------------------------- #
def site_statistics(con, cfg: SiteConfig) -> dict:
    """Upstream rescales fio2_set when its mean over the whole table exceeds 1."""
    mean = con.execute(f"SELECT avg(fio2_set) FROM read_parquet('{cfg.table_path('respiratory_support')}')").fetchone()[0]
    return {"fio2_recorded_as_percent": bool(mean is not None and mean > 1)}


def data_quality(con, cfg: SiteConfig) -> dict:
    def path(table):
        p = cfg.table_path(table)
        return f"read_parquet('{p}')" if p.exists() else None

    def scalar(sql):
        return int(con.execute(sql).fetchone()[0])

    semi_prone, prone_rows = [], 0
    if path("position"):
        semi_prone = sorted(r[0] for r in con.execute(
            f"SELECT DISTINCT CAST(position_name AS VARCHAR) FROM {path('position')} "
            "WHERE position_category = 'prone' AND CAST(position_name AS VARCHAR) ILIKE '%semi%'").fetchall())
        prone_rows = scalar(f"SELECT count(*) FROM {path('position')} WHERE position_category = 'prone'")
    assessments = path("patient_assessments")
    return {
        "semi_prone_names": semi_prone,
        "respiratory_rows_without_time": scalar(
            f"SELECT count(*) FROM {path('respiratory_support')} WHERE recorded_dttm IS NULL"),
        "prone_position_rows": prone_rows,
        "sat_documented_rows": scalar(
            f"SELECT count(*) FROM {assessments} WHERE assessment_category = 'sat_delivery_pass_fail'") if assessments else 0,
        "sbt_documented_rows": scalar(
            f"SELECT count(*) FROM {assessments} WHERE assessment_category = 'sbt_delivery_pass_fail'") if assessments else 0,
        # CLIF 3.0 values the 2.1 waterfall has no name for (src/clif_dash/vocabulary.py)
        "respiratory_rows_clif30_only": {f"{column} {value}": int(n) for column, value, n in con.execute(f"""
            SELECT 'device_category', lower(CAST(device_category AS VARCHAR)), count(*) FROM {path('respiratory_support')}
            WHERE lower(CAST(device_category AS VARCHAR)) = 't_piece' GROUP BY 2
            UNION ALL
            SELECT 'mode_category', lower(CAST(mode_category AS VARCHAR)), count(*) FROM {path('respiratory_support')}
            WHERE lower(CAST(mode_category AS VARCHAR)) IN ('aprv', 'avaps') GROUP BY 2""").fetchall()},
    }


# --------------------------------------------------------------------------- #
# Phase A: waterfall, IMV episodes, cohort exclusions (per chunk)
# --------------------------------------------------------------------------- #
def phase_a_chunk(cfg: SiteConfig, chunk: int, statistics: dict) -> dict:
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    work = cfg.stage_dir
    chunk_blocks = pd.read_parquet(work / "chunks_a.parquet").query("chunk == @chunk")["encounter_block"]
    mapping = pd.read_parquet(work / "mapping.parquet")
    mapping = mapping[mapping["encounter_block"].isin(chunk_blocks)].reset_index(drop=True)
    adt = pd.read_parquet(work / "adt.parquet")
    adt = adt[adt["encounter_block"].isin(chunk_blocks)].reset_index(drop=True)

    tables = {"respiratory_support": drop_untimed_rows(
        stage.load_staged_chunk(cfg, "phase_a", "respiratory_support", chunk))}
    outliers = clif_io.apply_outliers(tables, log=lambda m: None,
                                      fio2_percent=statistics["fio2_recorded_as_percent"])
    waterfall_fn, _, _ = clif_io.clifpy_functions()
    waterfall = prep.waterfall(tables, mapping, waterfall_fn)
    waterfall["recorded_dttm"] = waterfall["recorded_dttm"].dt.as_unit("us")  # same resolution as every staged table
    runs = prep.imv_runs(waterfall)
    funnel = {"encounters_with_imv": 0, "imv_episodes": 0, "not_icu": 0, "outside_intubation": 0, "outside_window": 0}
    if runs.empty:
        return {"chunk": chunk, "episodes": pd.DataFrame(), "runs": runs, "funnel": funnel, "outliers": outliers}
    episodes, runs = prep.episodes_from_runs(runs, shared["episode_gap_hours"])
    funnel["encounters_with_imv"] = int(episodes["encounter_block"].nunique())
    funnel["imv_episodes"] = int(len(episodes))

    episodes = prep.attribute_unit(episodes, adt)
    if shared["require_icu"]:
        before = len(episodes)
        episodes = episodes[episodes["icu_overlap"]]
        funnel["not_icu"] = before - len(episodes)
    first_location = adt.sort_values("in_dttm").groupby("encounter_block")["location_category"].first().str.lower()
    real_rows = waterfall[~waterfall["is_scaffold"] & waterfall["device_category"].notna()]
    first_device = real_rows.groupby("encounter_block")["device_category"].first()
    episodes["outside_intubation"] = (
        first_location.reindex(episodes["encounter_block"]).ne("ed").to_numpy()
        & first_device.reindex(episodes["encounter_block"]).eq("imv").to_numpy()
        & (episodes["episode_seq"] == 1).to_numpy()
    )
    if shared["exclude_outside_intubation"]:
        before = len(episodes)
        episodes = episodes[~episodes["outside_intubation"]]
        funnel["outside_intubation"] = before - len(episodes)
    if cfg.start_date or cfg.end_date:
        before = len(episodes)
        local_start = episodes["start"].dt.tz_convert(cfg.site_timezone)
        if cfg.start_date:
            episodes = episodes[local_start.dt.date >= pd.Timestamp(cfg.start_date).date()]
            local_start = episodes["start"].dt.tz_convert(cfg.site_timezone)
        if cfg.end_date:
            episodes = episodes[local_start.dt.date <= pd.Timestamp(cfg.end_date).date()]
        funnel["outside_window"] = before - len(episodes)

    # Keep the waterfall only for encounters still in the cohort; phase B and timelines read it.
    cohort_waterfall = waterfall[waterfall["encounter_block"].isin(episodes["encounter_block"])].copy()
    cohort_waterfall["waterfall_row"] = np.arange(len(cohort_waterfall))
    cohort_waterfall["phase_a_chunk"] = chunk
    destination = work / "waterfall_a"
    destination.mkdir(parents=True, exist_ok=True)
    cohort_waterfall.to_parquet(destination / f"chunk_{chunk}.parquet", index=False)
    return {"chunk": chunk, "episodes": episodes, "runs": runs, "funnel": funnel, "outliers": outliers}


# --------------------------------------------------------------------------- #
# Phase B: per-episode metrics and proning rows (per chunk)
# --------------------------------------------------------------------------- #
def phase_b_chunk(cfg: SiteConfig, chunk: int, statistics: dict) -> dict:
    decisions = cfg.decisions()
    work = cfg.stage_dir
    chunk_blocks = pd.read_parquet(work / "chunks_b.parquet").query("chunk == @chunk")["encounter_block"]
    in_chunk = lambda frame: frame[frame["encounter_block"].isin(chunk_blocks)].reset_index(drop=True)
    mapping = in_chunk(pd.read_parquet(work / "mapping.parquet"))
    adt = in_chunk(pd.read_parquet(work / "adt.parquet"))
    encounters = in_chunk(pd.read_parquet(work / "encounters.parquet"))
    episodes = in_chunk(pd.read_parquet(work / "episodes_a.parquet"))
    runs = in_chunk(pd.read_parquet(work / "runs_a.parquet"))
    waterfall = pd.read_parquet(work / "waterfall" / f"chunk_{chunk}.parquet")
    waterfall = waterfall.drop(columns=["waterfall_row", "phase_a_chunk", "chunk"]).reset_index(drop=True)

    tables = {t: stage.load_staged_chunk(cfg, "phase_b", t, chunk) for t in TABLES if t != "respiratory_support"}
    outliers = clif_io.apply_outliers(tables, log=lambda m: None,
                                      fio2_percent=statistics["fio2_recorded_as_percent"])
    hospital_diagnosis = tables.pop("hospital_diagnosis")
    charlson = charlson_with_log(cfg, chunk, hospital_diagnosis)

    context = dict(T=tables, enc=encounters, mapping=mapping, adt=adt, w=waterfall, runs=runs, ep=episodes,
                   cci=charlson)
    episode_metrics = metrics.compute(tables, context, decisions, cfg.site_timezone, log=lambda m: None,
                                      fit_mortality=False)
    context["ep"] = episode_metrics
    proning_rows, proning_funnel, _ = proning.compute(tables, context, decisions, log=lambda m: None, expected=False)

    labs = tables["labs"]
    abg_blocks = (set(labs[labs["lab_category"].eq("po2_arterial")].merge(mapping, on="hospitalization_id")["encounter_block"])
                  if not labs.empty else set())
    episode_metrics["has_abg"] = episode_metrics["encounter_block"].isin(abg_blocks)
    episode_metrics["has_weight"] = episode_metrics["encounter_block"].isin(context["weights"].index)
    paralytic_days = pd.DataFrame(
        [(episode_id, day_start, day_end, hours, flag)
         for episode_id, days in context["para_days"].items() for day_start, day_end, hours, flag in days],
        columns=["episode_id", "day_start", "day_end", "paralytic_hours", "over_threshold"])
    return {"chunk": chunk, "episodes": episode_metrics, "proning": proning_rows,
            "proning_funnel": proning_funnel, "paralytic_days": paralytic_days, "outliers": outliers,
            "ltvv_thresholds": context["ltvv_thresholds"]}


CCI_STACK_DUMP_SECONDS = 120


def charlson_with_log(cfg: SiteConfig, chunk: int, hospital_diagnosis: pd.DataFrame) -> pd.DataFrame:
    """The Charlson index for one chunk, logged step by step to stage/logs/cci_chunk_<n>.log.

    The log holds counts and versions only, no IDs or codes. If the step stalls, every
    CCI_STACK_DUMP_SECONDS the worker appends the Python stack of each of its threads,
    which shows where it waits.
    """
    folder = cfg.stage_dir / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"cci_chunk_{chunk}.log", "a", buffering=1) as handle:
        def note(message: str) -> None:
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} chunk {chunk} pid {os.getpid()} | {message}"
            handle.write(line + "\n")
            print(f"[CCI] {line}", flush=True)

        note(f"start method {multiprocessing.get_start_method(allow_none=True)}; "
             f"python {sys.version.split()[0]}; {package_versions('clifpy', 'polars', 'pandas', 'pyarrow')}")
        if hospital_diagnosis.empty:
            note("no hospital_diagnosis rows for this chunk")
        else:
            formats = hospital_diagnosis.get("diagnosis_code_format", pd.Series(dtype=str)).astype(str).str.lower()
            note(f"{len(hospital_diagnosis):,} diagnosis rows, "
                 f"{hospital_diagnosis['hospitalization_id'].nunique():,} hospitalizations; "
                 f"code formats {formats.value_counts().to_dict()}")
        faulthandler.dump_traceback_later(CCI_STACK_DUMP_SECONDS, repeat=True, file=handle)
        started = time.time()
        try:
            result = clif_io.charlson(hospital_diagnosis if not hospital_diagnosis.empty else None, log=note)
        finally:
            faulthandler.cancel_dump_traceback_later()
        note(f"done in {time.time() - started:.1f} s")
    return result


def package_versions(*names: str) -> str:
    def one(name):
        try:
            return f"{name} {version(name)}"
        except PackageNotFoundError:
            return f"{name} not installed"
    return ", ".join(one(name) for name in names)


def sum_outliers(results: list[dict]) -> dict:
    total: dict[str, int] = {}
    for r in results:
        for key, n in r["outliers"].items():
            total[key] = total.get(key, 0) + n
    return total


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def build(site: SiteBuild) -> BuildResult:
    cfg, con = site.cfg, site.con
    shared = shared_values(cfg.decisions())
    tz = cfg.site_timezone
    work = cfg.stage_dir
    encounters, mapping = site.encounters, site.mapping
    adults = encounters[encounters["adult"]]
    n_hospitalizations = int(stage.read_whole_table(cfg, "hospitalization")["hospitalization_id"].nunique())

    # ---- phase A ----------------------------------------------------------
    ventilated = ventilated_blocks(con, cfg, mapping)
    candidates = adults.loc[adults["encounter_block"].isin(ventilated), "encounter_block"]
    chunks_a = assign_chunks(candidates, cfg.encounters_per_chunk, cfg.workers)
    chunks_a.to_parquet(work / "chunks_a.parquet", index=False)
    staged = site.stage("respiratory_support", mapping.merge(chunks_a, on="encounter_block"), "phase_a")
    log(f"Phase A: {len(candidates):,} of {len(adults):,} adult encounters have a row that could be IMV; "
        f"{chunks_a['chunk'].nunique()} chunks, {staged:,} respiratory_support rows staged")
    results_a = site.run_chunks(phase_a_chunk, sorted(chunks_a["chunk"].unique()))
    episodes = pd.concat([r["episodes"] for r in results_a if len(r["episodes"])], ignore_index=True)
    runs = pd.concat([r["runs"] for r in results_a if len(r["runs"])], ignore_index=True)
    funnel_a = {k: sum(r["funnel"][k] for r in results_a) for k in results_a[0]["funnel"]}
    log(f"Phase A done: {len(episodes):,} IMV episodes in the cohort")

    funnel = [
        {"step": "Hospitalizations in CLIF", "n": n_hospitalizations},
        {"step": f"Encounters after linking (< {shared['stitch_hours']} h apart)", "n": int(len(encounters))},
        {"step": f"Adults (age ≥ {shared['age_min']})", "n": int(len(adults))},
        {"step": "Encounters with invasive ventilation", "n": funnel_a["encounters_with_imv"]},
        {"step": "IMV episodes in those encounters", "n": funnel_a["imv_episodes"], "unit": "episodes"},
    ]
    if shared["require_icu"]:
        funnel.append({"step": "Excluded: episode never in an ICU", "n": -funnel_a["not_icu"], "unit": "episodes"})
    if shared["exclude_outside_intubation"]:
        funnel.append({"step": "Excluded: intubated at outside hospital (first ADT not ED, first device IMV)",
                       "n": -funnel_a["outside_intubation"], "unit": "episodes"})
    if cfg.start_date or cfg.end_date:
        funnel.append({"step": "Outside the analysis window", "n": -funnel_a["outside_window"], "unit": "episodes"})
    funnel.append({"step": "IMV episodes analysed", "n": int(len(episodes)), "unit": "episodes"})

    # ---- phase B ----------------------------------------------------------
    cohort_blocks = episodes["encounter_block"].unique()
    chunks_b = assign_chunks(cohort_blocks, cfg.cohort_encounters_per_chunk, cfg.workers)
    chunks_b.to_parquet(work / "chunks_b.parquet", index=False)
    episodes.to_parquet(work / "episodes_a.parquet", index=False)
    runs[runs["encounter_block"].isin(cohort_blocks)].to_parquet(work / "runs_a.parquet", index=False)
    con.execute(f"""
        COPY (
            SELECT w.*, b.chunk FROM read_parquet('{work}/waterfall_a/*.parquet') w
            JOIN read_parquet('{work}/chunks_b.parquet') b USING (encounter_block)
            ORDER BY w.phase_a_chunk, w.waterfall_row
        ) TO '{work}/waterfall_sorted.parquet' (FORMAT parquet)
    """)
    (work / "waterfall").mkdir()
    sorted_waterfall = pd.read_parquet(work / "waterfall_sorted.parquet")
    for chunk, rows in sorted_waterfall.groupby("chunk", sort=True):
        rows.to_parquet(work / "waterfall" / f"chunk_{chunk}.parquet", index=False)
    del sorted_waterfall
    (work / "waterfall_sorted.parquet").unlink()

    cohort_map = mapping.merge(chunks_b, on="encounter_block")
    for table in HOSPITALIZATION_KEYED:
        if table != "respiratory_support":
            site.stage(table, cohort_map, "phase_b")
    patient_chunks = encounters[["encounter_block", "patient_id"]].merge(chunks_b, on="encounter_block")
    for table in PATIENT_KEYED:
        site.stage(table, patient_chunks, "phase_b")
    log(f"Phase B: {len(cohort_blocks):,} cohort encounters in {chunks_b['chunk'].nunique()} chunks staged")
    results_b = site.run_chunks(phase_b_chunk, sorted(chunks_b["chunk"].unique()))
    log("Phase B done")

    # ---- whole-cohort steps -------------------------------------------------
    episode_metrics = pd.concat([r["episodes"] for r in results_b], ignore_index=True)
    context = {}
    episode_metrics = metrics.fit_mortality_model(episode_metrics, context)
    proning_parts = [r["proning"] for r in results_b if len(r["proning"])]
    proning_rows = pd.concat(proning_parts, ignore_index=True) if proning_parts else pd.DataFrame()
    proning_info = {}
    if len(proning_rows):
        proning_rows, proning_info = proning.expected_proning(proning_rows)
    proning_funnel = [dict(step) for step in results_b[0]["proning_funnel"]]
    for i, step in enumerate(proning_funnel):
        step["n"] = int(sum(r["proning_funnel"][i]["n"] for r in results_b))

    encounter_index = encounters.set_index("encounter_block")
    blocks = episode_metrics["encounter_block"]
    episode_metrics["sex"] = blocks.map(encounter_index["sex_category"]).fillna("Unknown")
    episode_metrics["race_eth"] = [prep.race_eth(encounter_index.at[b, "race_category"],
                                                 encounter_index.at[b, "ethnicity_category"]) for b in blocks]
    episode_metrics["age_band"] = episode_metrics["age"].map(prep.age_band)
    episode_metrics["month"] = episode_metrics["start"].dt.tz_convert(tz).dt.strftime("%Y-%m")
    episode_metrics["label_id"] = blocks.map(encounter_index["hospitalization_id"])
    episode_metrics["discharge"] = blocks.map(encounter_index["discharge_category"]).fillna("Missing")
    episode_metrics["trach"] = episode_metrics["trach"].astype(bool)
    episode_metrics["has_height"] = episode_metrics["height_cm"].notna()
    episode_metrics["has_plateau"] = episode_metrics["plat_den"] > 0
    episode_metrics["has_rass"] = (episode_metrics["rass_den"] + episode_metrics["deep_den"]) > 0
    episode_metrics["has_vt"] = episode_metrics["vt_hours_total"] > 0
    ltvv_thresholds = results_b[0]["ltvv_thresholds"]
    columns = EPISODE_COLUMNS + [metrics.ltvv_key(t) for t in ltvv_thresholds] + COMPLETENESS_FLAGS
    episode_table = episode_metrics[columns].sort_values("episode_id").reset_index(drop=True)

    if len(proning_rows):
        strata = episode_metrics[["episode_id", "label_id", "hospital", "unit", "sex", "race_eth", "age_band", "code_status"]]
        proning_rows = proning_rows.merge(strata, on="episode_id", how="left")
        proning_rows["month"] = proning_rows["t_enroll"].dt.tz_convert(tz).dt.strftime("%Y-%m")

    outliers = sum_outliers(results_a)
    for key, n in sum_outliers(results_b).items():
        outliers[key] = outliers.get(key, 0) + n
    log(f"{len(episode_table):,} episodes, {len(proning_rows):,} proning rows")
    return BuildResult(
        tables={
            "episodes": episode_table,
            "proning": proning_rows,
            "paralytic_days": pd.concat([r["paralytic_days"] for r in results_b], ignore_index=True),
        },
        summary={"funnel": funnel, "proning_funnel": proning_funnel},
        meta={
            "months": sorted(episode_table["month"].dropna().unique().tolist()),
            "ltvv_thresholds": ltvv_thresholds,
            "proning_source": "CLIF_Proning_Incidence_Severe_ARF (commit 853338a), ported to Python",
        },
        data_quality={"outliers": outliers, "mortality_model": context.get("mort_model"),
                      # A site whose deaths are spelled otherwise reads 0% mortality: show what it records
                      "deaths_counted": int(episode_table["died"].sum()),
                      "discharge_categories": {str(k): int(n) for k, n in episode_table.loc[
                          episode_table["outcome_den"] == 1, "discharge"].value_counts().head(15).items()},
                      "proning_model": proning_info},
    )
