"""The chunked build must reproduce upstream's single-process output on the synthetic data.

tests/oracle/upstream_synth10k.json is upstream build_dashboard.py (commit b3cdf29)
run on ~/Research/synthetic_clif/synth_clif_10k with tests/oracle/upstream_synth10k_config.yaml.
Values are compared after upstream's own JSON formatting (_clean rounds floats to 4
places), with a small tolerance for floats whose last digit depends on summation order.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from clif_dash.build import build
from clif_dash.config import load_config

REPO = Path(__file__).resolve().parent.parent
ORACLE = json.loads((REPO / "tests" / "oracle" / "upstream_synth10k.json").read_text())
CONFIG = REPO / "config" / "synth_10k.yaml"
FLOAT_TOLERANCE = 2e-4


def upstream_clean(value):
    """build_dashboard._clean from upstream: the formatting the oracle went through."""
    if isinstance(value, dict):
        return {k: upstream_clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [upstream_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if (value is None or math.isnan(value) or math.isinf(value)) else round(float(value), 4)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, dt.date)):
        return str(value)
    if value is pd.NaT:
        return None
    return value


def local_minutes(series: pd.Series, tz: str) -> list:
    return [None if pd.isna(t) else pd.Timestamp(t).tz_convert(tz).strftime("%Y-%m-%d %H:%M") for t in series]


def assert_records_match(ours: dict, theirs: dict, what: str) -> None:
    assert set(ours) == set(theirs), f"{what}: ids differ"
    mismatches = []
    for record_id, expected in theirs.items():
        actual = ours[record_id]
        assert set(actual) == set(expected), f"{what} {record_id}: columns differ {set(actual) ^ set(expected)}"
        for column, want in expected.items():
            got = actual[column]
            if isinstance(want, float) and isinstance(got, (int, float)) and got is not None:
                if abs(got - want) > FLOAT_TOLERANCE * max(1.0, abs(want)):
                    mismatches.append((record_id, column, got, want))
            elif got != want:
                mismatches.append((record_id, column, got, want))
    assert not mismatches, f"{what}: {len(mismatches)} mismatches, first: {mismatches[:10]}"


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    cfg = load_config(CONFIG)
    if not cfg.clif_dir.exists():
        pytest.skip(f"synthetic CLIF data not found at {cfg.clif_dir}")
    cfg = replace(cfg, output_dir=tmp_path_factory.mktemp("synth_10k_build"))
    output = build(cfg)
    return cfg, output, json.loads((output / "summary.json").read_text())


def test_funnels_match(built):
    _, _, summary = built
    assert summary["funnel"] == ORACLE["funnel"]
    assert summary["proning_funnel"] == ORACLE["proning_funnel"]


def test_episode_rows_match(built):
    cfg, output, _ = built
    episodes = pd.read_parquet(output / "episodes.parquet").drop(columns=["encounter_block"])
    episodes["start"] = local_minutes(episodes["start"], cfg.site_timezone)
    episodes["end"] = local_minutes(episodes["end"], cfg.site_timezone)
    ours = {r["episode_id"]: r for r in upstream_clean(episodes.to_dict("records"))}
    theirs = {r["episode_id"]: r for r in ORACLE["episodes"]}
    assert_records_match(ours, theirs, "episodes")


def test_proning_rows_match(built):
    cfg, output, _ = built
    proning = pd.read_parquet(output / "proning.parquet").drop(columns=["encounter_block"])
    for column in ("t_enroll", "t_first_abg"):
        proning[column] = local_minutes(proning[column], cfg.site_timezone)
    ours = {r["episode_id"]: r for r in upstream_clean(proning.to_dict("records"))}
    theirs = {r["episode_id"]: r for r in ORACLE["proning"]}
    assert_records_match(ours, theirs, "proning")


def test_data_quality_matches(built):
    _, _, summary = built
    ours, theirs = summary["data_quality"], ORACLE["data_quality"]
    for key in ("row_counts", "semi_prone_names", "icu_rows_without_location_type", "prone_position_rows",
                "sat_documented_rows", "sbt_documented_rows", "proning_model"):
        assert upstream_clean(ours[key]) == theirs[key], key
    # Upstream counts outliers over whole tables; the chunked build sees only adult and cohort rows.
    assert sum(ours["outliers"].values()) <= sum(theirs["outliers"].values())
    model_ours, model_theirs = ours["mortality_model"], theirs["mortality_model"]
    assert (model_ours["n"], model_ours["events"], model_ours["terms"]) == (
        model_theirs["n"], model_theirs["events"], model_theirs["terms"])
    assert np.allclose(model_ours["coef"], model_theirs["coef"], atol=1e-4)
