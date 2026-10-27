"""The glycemic build on a hand-made site, with each day's counts worked out by hand.

Times are local (America/Los_Angeles, UTC-8 in winter) and stored in UTC.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from clif_dash.build import build
from clif_dash.config import load_config
from clif_dash.dashboards import GLYCEMIC

REPO = Path(__file__).resolve().parent.parent
TZ = "America/Los_Angeles"


def local(text: str):
    return pd.Timestamp(text, tz=TZ).tz_convert("UTC")


def write_site(folder: Path) -> Path:
    pd.DataFrame({"patient_id": ["P1", "P2", "P3"], "sex_category": ["Female"] * 3, "race_category": ["White"] * 3,
                  "ethnicity_category": ["Non-Hispanic"] * 3}).to_parquet(folder / "clif_patient.parquet", index=False)
    pd.DataFrame({
        "patient_id": ["P1", "P2", "P3"], "hospitalization_id": ["H1", "H2", "H3"],
        "admission_dttm": [local("2024-01-10 08:00"), local("2024-01-20 06:00"), local("2024-01-10 08:00")],
        "discharge_dttm": [local("2024-01-14 12:00"), local("2024-01-24 12:00"), local("2024-01-14 12:00")],
        "age_at_admission": [60, 70, 10], "admission_type_category": ["ed"] * 3, "discharge_category": ["Home"] * 3,
    }).to_parquet(folder / "clif_hospitalization.parquet", index=False)
    adt = pd.DataFrame([
        ("H1", "2024-01-10 12:00", "2024-01-12 09:00"),   # Jan 10, 11, 12
        ("H2", "2024-01-20 08:00", "2024-01-22 08:00"),   # Jan 20, 21, 22
        ("H3", "2024-01-10 12:00", "2024-01-12 09:00"),   # a child
    ], columns=["hospitalization_id", "in_dttm", "out_dttm"])
    adt["in_dttm"], adt["out_dttm"] = adt["in_dttm"].map(local), adt["out_dttm"].map(local)
    adt["location_category"], adt["location_type"], adt["location_name"] = "icu", "medical_icu", "MICU"
    adt["hospital_id"], adt["hospital_type"] = "HOSP-A", "academic"
    adt.to_parquet(folder / "clif_adt.parquet", index=False)

    labs = pd.DataFrame([
        ("H1", "2024-01-10 13:00", "glucose_serum", 35),          # day 10: below 40 and 70
        ("H1", "2024-01-10 20:00", "glucose_fingerstick", 150),   # day 10 mean 92.5: in range
        ("H1", "2024-01-11 08:00", "glucose_serum", 320),         # day 11: above 300
        ("H1", "2024-01-11 10:00", "glucose", 200),               # CLIF 3.0 point-of-care; day 11 mean 260: out of range
        ("H1", "2024-01-11 12:00", "glucose_fingerstick", 2500),  # outside clifpy's range: not counted
        ("H1", "2024-01-12 10:00", "glucose_serum", 30),          # after the ICU: not counted
        ("H1", "2024-01-11 09:00", "sodium", 20),                 # not glucose
        ("H2", "2024-01-20 18:00", "glucose_serum", 100),         # Jan 21 in UTC, Jan 20 here
        ("H2", "2024-01-21 01:00", "glucose_serum", 60),          # day 21: below 70
        ("H3", "2024-01-10 13:00", "glucose_serum", 30),          # a child
    ], columns=["hospitalization_id", "lab_collect_dttm", "lab_category", "lab_value_numeric"])
    labs["lab_collect_dttm"] = labs["lab_collect_dttm"].map(local)
    labs["lab_result_dttm"] = labs["lab_collect_dttm"] + pd.Timedelta(hours=1)
    labs["reference_unit"] = "mg/dL"
    labs.to_parquet(folder / "clif_labs.parquet", index=False)

    infusions = pd.DataFrame([
        # H1: runs Jan 10 22:00 to the stop on Jan 11 05:00: days 10 and 11
        ("H1", "2024-01-10 22:00", 2.0, "start"), ("H1", "2024-01-11 03:00", 3.0, "dose_change"),
        ("H1", "2024-01-11 05:00", 3.0, "stop"),
        # H2: one charted rate on Jan 20 at 22:00 runs 6 h, into Jan 21
        ("H2", "2024-01-20 22:00", 1.0, "going"),
    ], columns=["hospitalization_id", "admin_dttm", "med_dose", "mar_action_category"])
    infusions["admin_dttm"] = infusions["admin_dttm"].map(local)
    infusions["med_category"], infusions["med_dose_unit"], infusions["mar_action_group"] = \
        "insulin", "units/hr", "administered"
    infusions.to_parquet(folder / "clif_medication_admin_continuous.parquet", index=False)

    decisions = yaml.safe_load((REPO / "config" / "decisions" / "glycemic.yaml").read_text())
    for decision in decisions["shared"]:
        if decision["id"] == "glucose_coverage":
            decision["value"] = "all"
    (folder / "glycemic_decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True))
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"),
            "decisions": {"glycemic": str(folder / "glycemic_decisions.yaml")}, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


COLUMNS = ["days_present", "glucose_results", "glucose_days", "days_severe_hypo", "days_hypo", "days_severe_hyper",
           "days_in_range", "insulin_days", "insulin_glucose_days", "insulin_hypo_days"]
EXPECTED = {
    "H1": [3, 4, 2, 1, 1, 1, 1, 2, 2, 1],
    "H2": [3, 2, 2, 0, 1, 0, 1, 2, 2, 1],
}


def test_each_stay(tmp_path):
    output = build(load_config(write_site(tmp_path)), GLYCEMIC)
    rows = pd.read_parquet(output / "stay_months.parquet").set_index("label_id")
    assert sorted(rows.index) == sorted(EXPECTED)            # the child (H3) makes no row
    for key, expected in EXPECTED.items():
        assert rows.loc[key, COLUMNS].astype(int).tolist() == expected, key
