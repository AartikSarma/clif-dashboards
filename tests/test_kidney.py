"""The kidney build on a hand-made site, with KDIGO stages and CRRT doses worked out by hand.

The synthetic data's creatinine varies at random from day to day, so it meets
KDIGO criteria far more often than real data and cannot check them. Times are
local (America/Los_Angeles, UTC-8 in winter) and stored in UTC.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from clif_dash.build import build
from clif_dash.config import load_config
from clif_dash.dashboards import KIDNEY

REPO = Path(__file__).resolve().parent.parent
TZ = "America/Los_Angeles"


def local(text: str):
    return pd.Timestamp(text, tz=TZ).tz_convert("UTC")


def write_site(folder: Path) -> Path:
    pd.DataFrame({"patient_id": ["P1", "P2", "P3"], "sex_category": ["Female"] * 3, "race_category": ["White"] * 3,
                  "ethnicity_category": ["Non-Hispanic"] * 3}).to_parquet(folder / "clif_patient.parquet", index=False)
    pd.DataFrame({
        "patient_id": ["P1", "P2", "P3"], "hospitalization_id": ["H1", "H2", "H3"],
        "admission_dttm": [local("2024-01-09 08:00"), local("2024-01-10 06:00"), local("2024-01-10 06:00")],
        "discharge_dttm": [local("2024-01-18 12:00"), local("2024-01-15 12:00"), local("2024-01-15 12:00")],
        "age_at_admission": [60, 70, 10], "admission_type_category": ["ed"] * 3, "discharge_category": ["Home"] * 3,
    }).to_parquet(folder / "clif_hospitalization.parquet", index=False)
    adt = pd.DataFrame([("H1", "2024-01-10 08:00", "2024-01-16 08:00"),    # Jan 10-16: 7 days
                        ("H2", "2024-01-10 08:00", "2024-01-13 08:00"),    # Jan 10-13: 4 days
                        ("H3", "2024-01-10 08:00", "2024-01-13 08:00")],   # a child
                       columns=["hospitalization_id", "in_dttm", "out_dttm"])
    adt["in_dttm"], adt["out_dttm"] = adt["in_dttm"].map(local), adt["out_dttm"].map(local)
    adt["location_category"], adt["location_type"], adt["location_name"] = "icu", "medical_icu", "MICU"
    adt["hospital_id"], adt["hospital_type"] = "HOSP-A", "academic"
    adt.to_parquet(folder / "clif_adt.parquet", index=False)

    labs = pd.DataFrame([
        ("H1", "2024-01-09 10:00", 1.0),    # before the ICU: part of the baseline
        ("H1", "2024-01-10 10:00", 1.1),
        ("H1", "2024-01-11 10:00", 1.25),
        ("H1", "2024-01-12 10:00", 1.35),   # 0.35 above the Jan 9 result, but that is 72 h back: not AKI
        ("H1", "2024-01-13 10:00", 1.5),    # 1.5 × the 7-day low (1.0): stage 1 on ICU day 4
        ("H1", "2024-01-14 10:00", 2.0),    # 2.0 ×: stage 2 on ICU day 5
        ("H1", "2024-01-15 10:00", 2.5),
        ("H2", "2024-01-10 09:00", 1.0),
        ("H2", "2024-01-11 09:00", 1.3),    # exactly 0.3 in 24 h: stage 1, but on ICU day 2, so never counted
        ("H2", "2024-01-12 09:00", 3.1),    # 3.1 ×: stage 3 on ICU day 3, counted as stage 2 or 3
        ("H3", "2024-01-12 09:00", 3.1),    # a child
    ], columns=["hospitalization_id", "lab_collect_dttm", "lab_value_numeric"])
    labs["lab_collect_dttm"] = labs["lab_collect_dttm"].map(local)
    labs["lab_result_dttm"] = labs["lab_collect_dttm"] + pd.Timedelta(hours=1)
    labs["lab_category"], labs["reference_unit"] = "creatinine", "mg/dL"
    labs.to_parquet(folder / "clif_labs.parquet", index=False)

    crrt = pd.DataFrame([
        # Jan 12: 1,600 mL/h twice (a missing dialysate rate counts as 0) over 80 kg: 20 mL/kg/h, at target
        ("H2", "2024-01-12 10:00", 1000.0, 500.0, 0.0, 100.0),
        ("H2", "2024-01-12 14:00", 1000.0, 500.0, None, 100.0),
        # Jan 13: 600 mL/h: 7.5 mL/kg/h
        ("H2", "2024-01-13 02:00", 500.0, None, None, 100.0),
    ], columns=["hospitalization_id", "recorded_dttm", "pre_filter_replacement_fluid_rate",
                "post_filter_replacement_fluid_rate", "dialysate_flow_rate", "ultrafiltration_out"])
    crrt["recorded_dttm"] = crrt["recorded_dttm"].map(local)
    crrt["crrt_mode_category"], crrt["blood_flow_rate"] = "cvvhdf", 200.0
    crrt.to_parquet(folder / "clif_crrt_therapy.parquet", index=False)
    vitals = pd.DataFrame({"hospitalization_id": ["H2", "H2"], "vital_category": ["weight_kg"] * 2,
                           "vital_value": [78.0, 82.0],
                           "recorded_dttm": [local("2024-01-10 09:00"), local("2024-01-12 09:00")]})
    vitals.to_parquet(folder / "clif_vitals.parquet", index=False)

    decisions = yaml.safe_load((REPO / "config" / "decisions" / "kidney.yaml").read_text())
    for decision in decisions["shared"]:
        if decision["id"] == "creatinine_coverage":
            decision["value"] = "all"
    (folder / "kidney_decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True))
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"),
            "decisions": {"kidney": str(folder / "kidney_decisions.yaml")}, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


COLUMNS = ["days_present", "creatinine_days", "aki_events", "severe_aki_events", "crrt_days", "crrt_dose_days",
           "crrt_dose_target_days"]
EXPECTED = {
    "H1": [7, 6, 1, 1, 0, 0, 0],
    "H2": [4, 3, 0, 1, 2, 2, 1],
}


def test_each_stay(tmp_path):
    output = build(load_config(write_site(tmp_path)), KIDNEY)
    rows = pd.read_parquet(output / "stay_months.parquet").set_index("label_id")
    assert sorted(rows.index) == sorted(EXPECTED)            # the child (H3) makes no row
    for key, expected in EXPECTED.items():
        assert rows.loc[key, COLUMNS].astype(int).tolist() == expected, key
    assert rows.loc["H2", "crrt_dose_mean"] == 13.8          # (20 + 7.5) / 2, rounded
