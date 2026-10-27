"""The ICU flow build on a hand-made site, with each encounter's values worked out by hand.

Times are local (America/Los_Angeles, UTC-8 in winter) and stored in UTC.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from clif_dash.build import build
from clif_dash.config import load_config
from clif_dash.dashboards import FLOW

REPO = Path(__file__).resolve().parent.parent
TZ = "America/Los_Angeles"


def local(text: str | None):
    return pd.Timestamp(text, tz=TZ).tz_convert("UTC") if text else pd.NaT


def write_site(folder: Path) -> Path:
    ids = ["H1", "H2", "H3", "H4", "H5"]
    pd.DataFrame({"patient_id": [f"P{i}" for i in range(1, 6)], "sex_category": ["Female"] * 5,
                  "race_category": ["White"] * 5, "ethnicity_category": ["Non-Hispanic"] * 5}
                 ).to_parquet(folder / "clif_patient.parquet", index=False)
    pd.DataFrame({
        "patient_id": [f"P{i}" for i in range(1, 6)], "hospitalization_id": ids,
        "admission_dttm": [local(t) for t in ("2024-01-10 10:00", "2024-01-03 08:00", "2024-01-20 08:00",
                                              "2024-01-10 10:00", "2024-01-31 20:00")],
        "discharge_dttm": [local(t) for t in ("2024-01-16 12:00", "2024-01-12 12:00", "2024-01-22 06:00",
                                              "2024-01-12 12:00", "2024-02-03 12:00")],
        "age_at_admission": [60, 70, 80, 10, 50], "admission_type_category": ["ed"] * 5,
        "discharge_category": ["Home", "Home", "Expired", "Home", "Home"],
    }).to_parquet(folder / "clif_hospitalization.parquet", index=False)
    adt = pd.DataFrame([
        # H1: two ED rows (4 h from arrival), an ICU stay left at 23:30, back in the ICU 10.5 h later
        ("H1", "ed", None, "2024-01-10 10:00", "2024-01-10 12:00"),
        ("H1", "ed", None, "2024-01-10 12:00", "2024-01-10 14:00"),
        ("H1", "icu", "medical_icu", "2024-01-10 14:00", "2024-01-12 23:30"),   # 57.5 h
        ("H1", "ward", None, "2024-01-12 23:30", "2024-01-13 10:00"),
        ("H1", "icu", "medical_icu", "2024-01-13 10:00", "2024-01-14 10:00"),   # 24 h
        ("H1", "ward", None, "2024-01-14 10:00", "2024-01-16 12:00"),
        # H2: ED, then a ward, then the ICU (not from the ED); out at 15:00, back 49 h later (not a readmission)
        ("H2", "ed", None, "2024-01-03 08:00", "2024-01-03 10:00"),
        ("H2", "ward", None, "2024-01-03 10:00", "2024-01-05 08:00"),
        ("H2", "icu", "surgical_icu", "2024-01-05 08:00", "2024-01-06 15:00"),  # 31 h
        ("H2", "ward", None, "2024-01-06 15:00", "2024-01-08 16:00"),
        ("H2", "icu", "surgical_icu", "2024-01-08 16:00", "2024-01-09 16:00"),  # 24 h
        ("H2", "ward", None, "2024-01-09 16:00", "2024-01-12 12:00"),
        # H3: dies in the ICU
        ("H3", "icu", "medical_icu", "2024-01-20 08:00", "2024-01-22 06:00"),   # 46 h
        # H4: a child
        ("H4", "icu", "medical_icu", "2024-01-10 10:00", "2024-01-12 12:00"),
        # H5: from the ED at 23:00 on Jan 31 local (Feb 1 in UTC), discharged home from the ICU; no out time
        ("H5", "ed", None, "2024-01-31 20:00", "2024-01-31 23:00"),
        ("H5", "icu", "medical_icu", "2024-01-31 23:00", None),                 # to discharge: 61 h
    ], columns=["hospitalization_id", "location_category", "location_type", "in_dttm", "out_dttm"])
    adt["in_dttm"], adt["out_dttm"] = adt["in_dttm"].map(local), adt["out_dttm"].map(local)
    adt["hospital_id"], adt["hospital_type"] = "HOSP-A", "academic"
    adt["location_name"] = adt["location_type"].fillna(adt["location_category"])
    adt.to_parquet(folder / "clif_adt.parquet", index=False)

    decisions = yaml.safe_load((REPO / "config" / "decisions" / "flow.yaml").read_text())
    for decision in decisions["shared"]:
        if decision["id"] == "icu_coverage":
            decision["value"] = "all"
    (folder / "flow_decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True))
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"),
            "decisions": {"flow": str(folder / "flow_decisions.yaml")}, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


COLUMNS = ["month", "unit", "icu_days", "transferred_out", "readmitted_48h", "night_transfer_out", "from_ed",
           "ed_to_icu_hours", "died_in_icu", "discharged_from_icu"]
EXPECTED = {
    "H1": ["2024-01", "Medical ICU", round(81.5 / 24, 2), 1, 1, 1, 1, 4.0, 0, 0],
    "H2": ["2024-01", "Surgical ICU", round(55 / 24, 2), 1, 0, 0, 0, None, 0, 0],
    "H3": ["2024-01", "Medical ICU", round(46 / 24, 2), 0, 0, 0, 0, None, 1, 0],
    "H5": ["2024-01", "Medical ICU", round(61 / 24, 2), 0, 0, 0, 1, 3.0, 0, 1],
}


def test_each_encounter(tmp_path):
    output = build(load_config(write_site(tmp_path)), FLOW)
    rows = pd.read_parquet(output / "encounters.parquet").set_index("label_id")
    assert sorted(rows.index) == sorted(EXPECTED)            # the child (H4) makes no row
    for key, expected in EXPECTED.items():
        values = [None if pd.isna(v) else v for v in rows.loc[key, COLUMNS].tolist()]
        assert values == expected, key
