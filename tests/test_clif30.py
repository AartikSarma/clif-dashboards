"""A CLIF 3.0 site builds the same respiratory dashboard as the same site in CLIF 2.1.

clifpy's own 2.1 -> 3.0 crosswalk rewrites the synthetic data's category values
(IMV -> imv, Assist Control-Volume Control -> acvc, Expired -> expired, ...),
which is all CLIF 3.0 changed in the tables the dashboards read. The build of
that copy must still reproduce upstream's output. Before the dashboards read
3.0 values back as 2.1 (src/clif_dash/vocabulary.py), it silently lost most
ventilator hours, every SBT, every death and every code status.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pandas as pd
import pytest

from clif_dash.build import build
from test_oracle import ORACLE, assert_records_match, local_minutes, upstream_clean


@pytest.fixture(scope="module")
def built_clif30(synthetic_config, tmp_path_factory):
    from clifpy.utils.crosswalk import BETA_TABLES, crosswalk_file_2_1_to_3_0

    folder = tmp_path_factory.mktemp("synth_10k_clif30")
    for source in synthetic_config.clif_dir.glob(f"{synthetic_config.file_prefix}*.parquet"):
        table = source.stem[len(synthetic_config.file_prefix):]
        if table in BETA_TABLES:
            crosswalk_file_2_1_to_3_0(str(source), str(folder / source.name), table)
        else:
            shutil.copy(source, folder / source.name)
    cfg = replace(synthetic_config, clif_dir=folder, output_dir=tmp_path_factory.mktemp("synth_10k_clif30_build"))
    output = build(cfg)
    return cfg, output, json.loads((output / "summary.json").read_text())


def test_the_copy_really_is_clif30(built_clif30):
    cfg, _, _ = built_clif30
    devices = pd.read_parquet(cfg.table_path("respiratory_support"), columns=["device_category"])["device_category"]
    assert "imv" in set(devices) and "IMV" not in set(devices)


def test_funnels_and_rows_match_upstream(built_clif30):
    cfg, output, summary = built_clif30
    assert summary["funnel"] == ORACLE["funnel"]
    assert summary["proning_funnel"] == ORACLE["proning_funnel"]
    episodes = pd.read_parquet(output / "episodes.parquet").drop(columns=["encounter_block"])
    episodes["start"] = local_minutes(episodes["start"], cfg.site_timezone)
    episodes["end"] = local_minutes(episodes["end"], cfg.site_timezone)
    assert_records_match({r["episode_id"]: r for r in upstream_clean(episodes.to_dict("records"))},
                         {r["episode_id"]: r for r in ORACLE["episodes"]}, "episodes")
