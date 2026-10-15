"""Small checks on build steps that the synthetic data does not exercise."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from clif_dash.build import drop_untimed_rows
from clif_dash.upstream import clif_io, prep

SYNTHETIC = Path("~/Research/synthetic_clif/synth_clif_10k").expanduser()


@pytest.mark.skipif(not SYNTHETIC.exists(), reason="synthetic CLIF data not found")
def test_untimed_ventilator_rows_do_not_reach_the_waterfall():
    """Rows without recorded_dttm pass through clifpy's waterfall with a missing time,
    which later breaks proning's time-ordered join; the build drops them first."""
    rows = pd.read_parquet(SYNTHETIC / "clif_respiratory_support.parquet")
    rows = rows[rows["hospitalization_id"].isin(rows["hospitalization_id"].unique()[:50])].reset_index(drop=True)
    rows.loc[rows.sample(5, random_state=1).index, "recorded_dttm"] = pd.NaT
    mapping = pd.DataFrame({"hospitalization_id": rows["hospitalization_id"].unique()})
    mapping["encounter_block"] = range(len(mapping))
    waterfall_fn, _, _ = clif_io.clifpy_functions()

    unguarded = prep.waterfall({"respiratory_support": rows}, mapping, waterfall_fn)
    assert unguarded["recorded_dttm"].isna().any()  # the failure this guards against

    guarded = prep.waterfall({"respiratory_support": drop_untimed_rows(rows)}, mapping, waterfall_fn)
    assert guarded["recorded_dttm"].notna().all()
