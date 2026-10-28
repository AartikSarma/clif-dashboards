"""The fluids build on a hand-made CLIF 3.0 site, with volumes and ICU days worked out by hand.

The synthetic data is CLIF 2.1 and has no `input` table, so these encounters are
the only test. Times are local (America/Los_Angeles); the input table stores them
as UTC timestamps without a zone, which the build must read as UTC.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from clif_dash.build import build, build_all
from clif_dash.config import load_config
from clif_dash.dashboards import FLOW, FLUIDS, STEWARDSHIP

REPO = Path(__file__).resolve().parent.parent
TZ = "America/Los_Angeles"


def local(text: str):
    return pd.Timestamp(text, tz=TZ).tz_convert("UTC")


def write_site(folder: Path, with_input: bool = True) -> Path:
    pd.DataFrame({"patient_id": ["P1", "P2", "P3", "P4"], "sex_category": ["Female"] * 4,
                  "race_category": ["White"] * 4, "ethnicity_category": ["Non-Hispanic"] * 4}
                 ).to_parquet(folder / "clif_patient.parquet", index=False)
    pd.DataFrame({
        "patient_id": ["P1", "P2", "P3", "P4"], "hospitalization_id": ["H1", "H2", "H3", "H4"],
        "admission_dttm": [local("2024-03-01 08:00"), local("2024-03-09 20:00"), local("2024-03-31 18:00"),
                           local("2024-03-01 08:00")],
        "discharge_dttm": [local("2024-03-05 12:00"), local("2024-03-12 12:00"), local("2024-04-02 12:00"),
                           local("2024-03-03 12:00")],
        "age_at_admission": [60, 70, 50, 10], "admission_type_category": ["ed"] * 4, "discharge_category": ["Home"] * 4,
    }).to_parquet(folder / "clif_hospitalization.parquet", index=False)
    adt = pd.DataFrame([
        ("H1", "2024-03-01 10:00", "2024-03-03 10:00"),   # 14 + 24 + 10 = 48 h: 2 ICU days
        ("H2", "2024-03-09 22:00", "2024-03-11 02:00"),   # across the spring-forward night: 2 + 23 + 2 = 27 h
        ("H3", "2024-03-31 20:00", "2024-04-01 04:00"),   # 4 h in March, 4 h in April
        ("H4", "2024-03-01 10:00", "2024-03-02 10:00"),   # a child
    ], columns=["hospitalization_id", "in_dttm", "out_dttm"])
    adt["in_dttm"], adt["out_dttm"] = adt["in_dttm"].map(local), adt["out_dttm"].map(local)
    adt["location_category"], adt["location_type"], adt["location_name"] = "icu", "medical_icu", "MICU"
    adt["hospital_id"], adt["hospital_type"] = "HOSP-A", "academic"
    adt.to_parquet(folder / "clif_adt.parquet", index=False)
    if with_input:
        rows = pd.DataFrame([
            ("H1", "2024-03-01 09:30", "iv_crystalloid", "iv_fluids", 400),          # before the ICU: not counted
            ("H1", "2024-03-01 12:00", "iv_crystalloid", "iv_fluids", 1000),
            ("H1", "2024-03-01 20:00", "iv_colloid", "iv_fluids", 250),
            ("H1", "2024-03-02 08:00", "enteral_nutrition", "nutrition", 500),
            ("H1", "2024-03-02 09:00", "blood_products", "blood_products", 300),
            ("H1", "2024-03-02 10:00", "flush_irrigation_urine", "flush_irrigation", 200),
            ("H1", "2024-03-02 11:00", "iv_crystalloid", "iv_fluids", -50),           # negative: not counted
            ("H1", "2024-03-02 12:00", "renal_replacement_fluids", "renal_replacement_fluids", 20000),  # a day's CRRT: under its limit
            ("H1", "2024-03-03 09:00", "iv_medication", "medication_fluids", 100),
            ("H1", "2024-03-03 11:00", "iv_crystalloid", "iv_fluids", 1000),         # after the ICU: not counted
            ("H2", "2024-03-10 12:00", "iv_crystalloid", "iv_fluids", 500),
            ("H2", "2024-03-10 13:00", "iv_crystalloid", "iv_fluids", 2_000_000),    # above the limit: not counted
            ("H3", "2024-04-01 01:00", "other", "other", 100),
            ("H4", "2024-03-01 12:00", "iv_crystalloid", "iv_fluids", 999),          # a child
        ], columns=["hospitalization_id", "recorded_dttm", "input_category", "input_group", "input_volume"])
        # UTC without a zone, as some sites write CLIF datetimes
        rows["recorded_dttm"] = rows["recorded_dttm"].map(local).dt.tz_localize(None)
        rows["input_name"] = "site label, never read"
        rows.to_parquet(folder / "clif_input.parquet", index=False)
    decisions = yaml.safe_load((REPO / "config" / "decisions" / "fluids.yaml").read_text())
    for decision in decisions["shared"]:
        if decision["id"] == "input_coverage":
            decision["value"] = "all"
    (folder / "fluids_decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True))
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"),
            "decisions": {"fluids": str(folder / "fluids_decisions.yaml")}, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build(load_config(write_site(tmp_path_factory.mktemp("fluids_site"))), FLUIDS)


@pytest.fixture(scope="module")
def stay_months(built):
    return pd.read_parquet(built / "stay_months.parquet").set_index(["label_id", "month"])


COLUMNS = ["vol_total", "vol_iv_fluids", "vol_iv_crystalloid", "vol_iv_colloid", "vol_blood_products",
           "vol_nutrition", "vol_medication_fluids", "vol_renal_replacement_fluids", "vol_flush_irrigation",
           "vol_other"]
EXPECTED = {
    ("H1", "2024-03"): (2.0, [22350, 1250, 1000, 250, 300, 500, 100, 20000, 200, 0]),
    ("H2", "2024-03"): (27 / 24, [500, 500, 500, 0, 0, 0, 0, 0, 0, 0]),
    ("H3", "2024-03"): (4 / 24, [0] * 10),
    ("H3", "2024-04"): (4 / 24, [100, 0, 0, 0, 0, 0, 0, 0, 0, 100]),
}


def test_volumes_and_icu_days(stay_months):
    assert set(stay_months.index) == set(EXPECTED)            # the child (H4) makes no row
    for key, (icu_days, volumes) in EXPECTED.items():
        assert stay_months.loc[key, "icu_days"] == pytest.approx(icu_days), key
        assert stay_months.loc[key, COLUMNS].astype(float).tolist() == volumes, key


def test_volumes_above_their_group_limit_are_counted_as_data_quality(built):
    quality = json.loads((built / "summary.json").read_text())["data_quality"]
    assert quality["input_rows_over_limit"] == {"iv_fluids": {"rows": 1, "litres": 2000.0}}


def test_a_dashboard_whose_tables_are_missing_is_skipped_with_a_message(tmp_path, capsys):
    cfg = load_config(write_site(tmp_path, with_input=False))
    built = build_all(cfg, [STEWARDSHIP, FLUIDS, FLOW])        # flow needs only ADT, which this site has
    assert list(built) == ["flow"]
    log = capsys.readouterr().out
    assert "Could not build stewardship" in log and "clif_medication_admin_intermittent.parquet" in log
    assert "Could not build fluids" in log and "clif_input.parquet" in log
    with pytest.raises(FileNotFoundError, match="fluids"):    # asking for one dashboard alone still raises
        build(cfg, FLUIDS)
