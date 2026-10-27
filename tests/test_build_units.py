"""Small checks on build steps that the synthetic data does not exercise."""
from __future__ import annotations

import pandas as pd
import pytest

from clif_dash.dashboards.respiratory.build import drop_untimed_rows
from clif_dash.upstream import clif_io, prep

def test_untimed_ventilator_rows_do_not_reach_the_waterfall(synthetic_config):
    """Rows without recorded_dttm pass through clifpy's waterfall with a missing time,
    which later breaks proning's time-ordered join; the build drops them first."""
    rows = pd.read_parquet(synthetic_config.table_path("respiratory_support"))
    rows = rows[rows["hospitalization_id"].isin(rows["hospitalization_id"].unique()[:50])].reset_index(drop=True)
    rows.loc[rows.sample(5, random_state=1).index, "recorded_dttm"] = pd.NaT
    mapping = pd.DataFrame({"hospitalization_id": rows["hospitalization_id"].unique()})
    mapping["encounter_block"] = range(len(mapping))
    waterfall_fn, _, _ = clif_io.clifpy_functions()

    unguarded = prep.waterfall({"respiratory_support": rows}, mapping, waterfall_fn)
    assert unguarded["recorded_dttm"].isna().any()  # the failure this guards against

    guarded = prep.waterfall({"respiratory_support": drop_untimed_rows(rows)}, mapping, waterfall_fn)
    assert guarded["recorded_dttm"].notna().all()


def test_datetimes_share_one_resolution_however_stored():
    """Text datetimes parse to nanoseconds and parquet timestamps load as microseconds;
    proning's merge_asof between labs and the waterfall fails unless they match."""
    from clif_dash.stage import clif_conventions

    as_text = clif_conventions(pd.DataFrame({"lab_result_dttm": ["2022-06-24 13:18:00+0000", None]}))
    as_nanoseconds = clif_conventions(pd.DataFrame(
        {"recorded_dttm": pd.to_datetime(["2022-06-24 12:00:00"], utc=True).as_unit("ns")}))
    assert str(as_text["lab_result_dttm"].dtype) == "datetime64[us, UTC]"
    assert str(as_nanoseconds["recorded_dttm"].dtype) == "datetime64[us, UTC]"
    joined = pd.merge_asof(as_text.dropna(), as_nanoseconds, left_on="lab_result_dttm",
                           right_on="recorded_dttm", direction="backward")
    assert joined["recorded_dttm"].notna().all()


def test_staging_can_keep_only_the_rows_a_dashboard_reads(tmp_path):
    """A `where` condition narrows the staged rows; source order and chunks are kept."""
    from clif_dash.config import load_config
    from clif_dash.stage import connect, load_staged_chunk, stage_table

    rows = pd.DataFrame({"hospitalization_id": ["a", "b", "a", "c", "b"],
                         "med_category": ["Cefepime", "acetaminophen", "vancomycin", "cefepime", "meropenem"]})
    rows.to_parquet(tmp_path / "clif_medication_admin_intermittent.parquet", index=False)
    (tmp_path / "site.yaml").write_text(f"clif_dir: {tmp_path}\noutput_dir: {tmp_path / 'out'}\n")
    cfg = load_config(tmp_path / "site.yaml").bind("test")
    chunk_map = pd.DataFrame({"hospitalization_id": ["a", "b"], "chunk": [0, 1]})
    staged = stage_table(connect(), cfg, "medication_admin_intermittent", chunk_map,
                         cfg.stage_dir / "meds" / "medication_admin_intermittent",
                         where="lower(source.med_category) IN ('cefepime', 'vancomycin', 'meropenem')")
    assert staged == 3
    first = load_staged_chunk(cfg, "meds", "medication_admin_intermittent", 0)
    assert first["med_category"].tolist() == ["Cefepime", "vancomycin"]
    assert load_staged_chunk(cfg, "meds", "medication_admin_intermittent", 1)["med_category"].tolist() == ["meropenem"]


def test_the_build_log_works_without_the_resource_module(monkeypatch, capsys):
    """Windows has no resource module; the log line then leaves out peak memory instead of failing."""
    from clif_dash import dashboard

    monkeypatch.setattr(dashboard, "resource", None)
    dashboard.log("Phase A")
    line = capsys.readouterr().out
    assert line.endswith("] Phase A\n") and "peak" not in line


def test_workers_are_spawned_not_forked(monkeypatch):
    """Linux forks by default, and a forked worker hangs in its first polars call (clifpy's
    Charlson index): it inherits the parent's thread pool without its threads."""
    from clif_dash import dashboard

    seen = {}

    class Recorder:
        def __init__(self, max_workers, mp_context=None):
            seen["method"] = mp_context.get_start_method() if mp_context else None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, function, *iterables):
            return map(function, *iterables)

    monkeypatch.setattr(dashboard, "ProcessPoolExecutor", Recorder)
    cfg = type("Config", (), {"workers": 2})()
    site = dashboard.SiteBuild(cfg=cfg, con=None, encounters=None, mapping=None, adt=None, site_statistics={})
    assert site.run_chunks(lambda cfg, chunk, statistics: chunk, [0, 1]) == [0, 1]
    assert seen["method"] == "spawn"


def test_the_charlson_step_writes_a_log_and_dumps_stacks_when_it_stalls(tmp_path, monkeypatch):
    """Each chunk's Charlson step is logged; if it stalls, the worker appends its thread stacks."""
    import time

    from clif_dash.config import load_config
    from clif_dash.dashboards.respiratory import build as respiratory

    (tmp_path / "site.yaml").write_text(f"clif_dir: {tmp_path}\noutput_dir: {tmp_path / 'out'}\n")
    cfg = load_config(tmp_path / "site.yaml").bind("respiratory")
    diagnoses = pd.DataFrame({"hospitalization_id": ["a", "a", "b"], "diagnosis_code": ["I50.9", "E11.9", "J18.9"],
                              "diagnosis_code_format": ["ICD10CM"] * 3})
    scored = respiratory.charlson_with_log(cfg, 7, diagnoses)
    assert set(scored["hospitalization_id"]) == {"a", "b"}
    log = (cfg.stage_dir / "logs" / "cci_chunk_7.log").read_text()
    assert "start method" in log and "3 diagnosis rows, 2 hospitalizations" in log and "finished" in log
    assert "I50" not in log                    # counts and versions only, never codes

    def slow(hospital_diagnosis, log):
        time.sleep(2.5)
        return pd.DataFrame(columns=["hospitalization_id", "cci_score"])
    monkeypatch.setattr(respiratory, "CCI_STACK_DUMP_SECONDS", 1)
    monkeypatch.setattr(respiratory.clif_io, "charlson", slow)
    respiratory.charlson_with_log(cfg, 8, diagnoses)
    assert "Thread" in (cfg.stage_dir / "logs" / "cci_chunk_8.log").read_text()
