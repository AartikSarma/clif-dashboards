"""Batch build: CLIF tables -> episode-level and proning-level metric tables.

The metric logic is upstream's (src/clif_dash/upstream, from
cag-lab/clif-icu-quality-dashboard at b3cdf29). What changes is how data reach it:

1. Small tables (patient, hospitalization, adt) are read whole, and encounters
   are stitched across the site.
2. Phase A. respiratory_support for adult encounters is staged in chunks; each
   worker runs the clifpy waterfall and finds IMV episodes for its chunk, then
   applies the cohort exclusions (ICU, outside-hospital intubation, date window).
3. Phase B. The large tables are staged only for encounters left in the cohort,
   in smaller chunks; each worker computes the per-episode metrics and the
   proning rows for its chunk.
4. Steps that need the whole cohort run last: the site-fitted mortality model,
   expected proning (BMI imputation uses the cohort median), and the data-quality
   summary, which DuckDB computes over the full source tables.

Every per-encounter computation sees exactly the rows it would have seen in the
single-process upstream build, which tests/test_oracle.py checks.
"""
from __future__ import annotations

import datetime as dt
import json
import resource
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from . import stage
from .config import SiteConfig
from .upstream import clif_io, metrics, prep, proning

PIPELINE_VERSION = "0.1.0"
UPSTREAM_COMMIT = "b3cdf29"
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


def log(message: str) -> None:
    peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9  # bytes on macOS
    print(f"[{time.strftime('%H:%M:%S')} | peak {peak_gb:.1f} GB] {message}", flush=True)


def shared_values(decisions: dict) -> dict:
    return {d["id"]: d["value"] for d in decisions["shared"]}


# --------------------------------------------------------------------------- #
# Encounters
# --------------------------------------------------------------------------- #
def build_encounters(cfg: SiteConfig, decisions: dict):
    shared = shared_values(decisions)
    small_tables = {t: stage.read_whole_table(cfg, t) for t in ("patient", "hospitalization", "adt")}
    _, stitch_fn, clifpy_source = clif_io.clifpy_functions()
    encounters, mapping, adt = prep.build_encounters(
        small_tables, stitch_fn, shared["stitch_hours"], shared["age_min"], log=lambda m: log(m.strip()))
    return encounters, mapping, adt, clifpy_source


def assign_chunks(encounter_blocks, encounters_per_chunk: int) -> pd.DataFrame:
    blocks = np.sort(np.asarray(pd.unique(encounter_blocks)))
    return pd.DataFrame({"encounter_block": blocks, "chunk": np.arange(len(blocks)) // encounters_per_chunk})


def drop_untimed_rows(respiratory_support: pd.DataFrame) -> pd.DataFrame:
    """Drop respiratory_support rows with no recorded_dttm before the waterfall.

    clifpy's waterfall carries such rows through with a missing time, and later
    time-ordered joins (proning's ABG-to-ventilator match, timelines) then fail.
    A setting with no time cannot be placed on the timeline, so dropping it loses
    nothing; data_quality reports how many rows the site has.
    """
    return respiratory_support[respiratory_support["recorded_dttm"].notna()].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Phase A: waterfall, IMV episodes, cohort exclusions (per chunk)
# --------------------------------------------------------------------------- #
def phase_a_chunk(cfg: SiteConfig, chunk: int, fio2_percent: bool) -> dict:
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
    outliers = clif_io.apply_outliers(tables, log=lambda m: None, fio2_percent=fio2_percent)
    waterfall_fn, _, _ = clif_io.clifpy_functions()
    waterfall = prep.waterfall(tables, mapping, waterfall_fn)
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
def phase_b_chunk(cfg: SiteConfig, chunk: int, fio2_percent: bool) -> dict:
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

    tables = {t: stage.load_staged_chunk(cfg, "phase_b", t, chunk)
              for t in stage.HOSPITALIZATION_KEYED + stage.PATIENT_KEYED if t != "respiratory_support"}
    outliers = clif_io.apply_outliers(tables, log=lambda m: None, fio2_percent=fio2_percent)
    hospital_diagnosis = tables.pop("hospital_diagnosis")
    charlson = clif_io.charlson(hospital_diagnosis if not hospital_diagnosis.empty else None)

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


def run_chunks(fn, cfg: SiteConfig, chunks, fio2_percent: bool) -> list[dict]:
    if cfg.workers <= 1:
        return [fn(cfg, c, fio2_percent) for c in chunks]
    with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
        return list(pool.map(fn, [cfg] * len(chunks), chunks, [fio2_percent] * len(chunks)))


# --------------------------------------------------------------------------- #
# Whole-site summaries (DuckDB over the full source tables)
# --------------------------------------------------------------------------- #
def data_quality(con, cfg: SiteConfig) -> dict:
    def path(table):
        p = cfg.table_path(table)
        return f"read_parquet('{p}')" if p.exists() else None

    def scalar(sql):
        return int(con.execute(sql).fetchone()[0])

    row_counts = {t: scalar(f"SELECT count(*) FROM {path(t)}") for t in stage.ALL_TABLES if path(t)}
    semi_prone, prone_rows = [], 0
    if path("position"):
        semi_prone = sorted(r[0] for r in con.execute(
            f"SELECT DISTINCT CAST(position_name AS VARCHAR) FROM {path('position')} "
            "WHERE position_category = 'prone' AND CAST(position_name AS VARCHAR) ILIKE '%semi%'").fetchall())
        prone_rows = scalar(f"SELECT count(*) FROM {path('position')} WHERE position_category = 'prone'")
    assessments = path("patient_assessments")
    return {
        "row_counts": row_counts,
        "semi_prone_names": semi_prone,
        "respiratory_rows_without_time": scalar(
            f"SELECT count(*) FROM {path('respiratory_support')} WHERE recorded_dttm IS NULL"),
        "icu_rows_without_location_type": scalar(
            f"SELECT count(*) FROM {path('adt')} WHERE lower(location_category) = 'icu' AND location_type IS NULL"),
        "prone_position_rows": prone_rows,
        "sat_documented_rows": scalar(
            f"SELECT count(*) FROM {assessments} WHERE assessment_category = 'sat_delivery_pass_fail'") if assessments else 0,
        "sbt_documented_rows": scalar(
            f"SELECT count(*) FROM {assessments} WHERE assessment_category = 'sbt_delivery_pass_fail'") if assessments else 0,
    }


def fio2_recorded_as_percent(con, cfg: SiteConfig) -> bool:
    """Upstream rescales fio2_set when its mean over the whole table exceeds 1."""
    mean = con.execute(f"SELECT avg(fio2_set) FROM read_parquet('{cfg.table_path('respiratory_support')}')").fetchone()[0]
    return bool(mean is not None and mean > 1)


def sum_outliers(results: list[dict]) -> dict:
    total: dict[str, int] = {}
    for r in results:
        for key, n in r["outliers"].items():
            total[key] = total.get(key, 0) + n
    return total


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def build(cfg: SiteConfig) -> Path:
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    tz = cfg.site_timezone
    work = cfg.stage_dir
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    con = stage.connect()
    started = time.time()

    log(f"Encounters from {cfg.clif_dir}")
    encounters, mapping, adt, clifpy_source = build_encounters(cfg, decisions)
    adults = encounters[encounters["adult"]]
    encounters.to_parquet(work / "encounters.parquet", index=False)
    mapping.to_parquet(work / "mapping.parquet", index=False)
    adt.to_parquet(work / "adt.parquet", index=False)
    n_hospitalizations = int(stage.read_whole_table(cfg, "hospitalization")["hospitalization_id"].nunique())

    # ---- phase A ----------------------------------------------------------
    chunks_a = assign_chunks(adults["encounter_block"], cfg.encounters_per_chunk)
    chunks_a.to_parquet(work / "chunks_a.parquet", index=False)
    fio2_percent = fio2_recorded_as_percent(con, cfg)
    staged = stage.stage_table(con, cfg, "respiratory_support", mapping.merge(chunks_a, on="encounter_block"),
                               work / "phase_a" / "respiratory_support")
    log(f"Phase A: {len(adults):,} adult encounters in {chunks_a['chunk'].nunique()} chunks; "
        f"{staged:,} respiratory_support rows staged")
    results_a = run_chunks(phase_a_chunk, cfg, sorted(chunks_a["chunk"].unique()), fio2_percent)
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
    chunks_b = assign_chunks(cohort_blocks, cfg.cohort_encounters_per_chunk)
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
    for table in stage.HOSPITALIZATION_KEYED:
        if table != "respiratory_support":
            stage.stage_table(con, cfg, table, cohort_map, work / "phase_b" / table)
    patient_chunks = encounters[["encounter_block", "patient_id"]].merge(chunks_b, on="encounter_block")
    stage.stage_table(con, cfg, "code_status", patient_chunks, work / "phase_b" / "code_status")
    log(f"Phase B: {len(cohort_blocks):,} cohort encounters in {chunks_b['chunk'].nunique()} chunks staged")
    results_b = run_chunks(phase_b_chunk, cfg, sorted(chunks_b["chunk"].unique()), fio2_percent)
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

    quality = data_quality(con, cfg)
    outliers = sum_outliers(results_a)
    for key, n in sum_outliers(results_b).items():
        outliers[key] = outliers.get(key, 0) + n
    quality.update(outliers=outliers, mortality_model=context.get("mort_model"), proning_model=proning_info)

    output = cfg.output_dir
    episode_table.to_parquet(output / "episodes.parquet", index=False)
    proning_rows.to_parquet(output / "proning.parquet", index=False)
    pd.concat([r["paralytic_days"] for r in results_b], ignore_index=True).to_parquet(
        output / "paralytic_days.parquet", index=False)
    summary = {
        "meta": {
            "site_name": cfg.site_name,
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_folder": cfg.clif_dir.name,
            "timezone": tz,
            "clifpy": clifpy_source,
            "pipeline_version": PIPELINE_VERSION,
            "upstream_commit": UPSTREAM_COMMIT,
            "months": sorted(episode_table["month"].dropna().unique().tolist()),
            "ltvv_thresholds": ltvv_thresholds,
            "proning_source": "CLIF_Proning_Incidence_Severe_ARF (commit 853338a), ported to Python",
            "fio2_recorded_as_percent": fio2_percent,
        },
        "decisions": decisions,
        "funnel": funnel,
        "proning_funnel": proning_funnel,
        "data_quality": quality,
    }
    (output / "summary.json").write_text(json.dumps(summary, default=_json_default, ensure_ascii=False, indent=1))
    log(f"Wrote {output} ({len(episode_table):,} episodes, {len(proning_rows):,} proning rows) "
        f"in {time.time() - started:.0f} s")
    return output


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, dt.date)):
        return str(value)
    raise TypeError(f"Not JSON serializable: {type(value)}")


if __name__ == "__main__":
    sys.exit("Run through the CLI: uv run clif-dash build --config <site.yaml>")
