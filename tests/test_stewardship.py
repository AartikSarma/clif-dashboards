"""The stewardship build on a hand-made CLIF site with counts worked out by hand.

There is no upstream for this dashboard, so a few encounters stand in for an
oracle, each built to test one counting rule. Times are written in the site's
local time (America/Los_Angeles, UTC-8 in winter) and stored in UTC, as CLIF does.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from clif_dash.build import build
from clif_dash.config import load_config
from clif_dash.dashboards import STEWARDSHIP

REPO = Path(__file__).resolve().parent.parent
TZ = "America/Los_Angeles"


def local(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=TZ).tz_convert("UTC")


def write_site(folder: Path) -> Path:
    patients = pd.DataFrame({
        "patient_id": ["P1", "P2", "P3", "P4", "P5"],
        "sex_category": ["Female", "Male", "Female", "Male", "Female"],
        "race_category": ["White"] * 5, "ethnicity_category": ["Non-Hispanic"] * 5,
    })
    hospitalizations = pd.DataFrame({
        "patient_id": ["P1", "P2", "P3", "P4", "P5"],
        "hospitalization_id": ["H1", "H2", "H3", "H4", "H5"],
        "admission_dttm": [local("2024-01-10 10:00"), local("2024-01-31 05:00"), local("2024-01-10 10:00"),
                           local("2024-03-05 09:00"), local("2024-04-01 09:00")],
        "discharge_dttm": [local("2024-01-14 12:00"), local("2024-02-04 12:00"), local("2024-01-12 12:00"),
                           local("2024-03-06 12:00"), local("2024-04-03 12:00")],
        "age_at_admission": [60, 70, 10, 50, 40],
        "admission_type_category": ["ed"] * 5,
        "discharge_category": ["Home"] * 5,
    })
    adt_rows = [
        # H1: ED, then the Medical ICU across two midnights (Jan 10, 11, 12), then a ward
        ("H1", "ed", None, "2024-01-10 10:00", "2024-01-10 12:00"),
        ("H1", "icu", "medical_icu", "2024-01-10 12:00", "2024-01-12 09:00"),
        ("H1", "ward", None, "2024-01-12 09:00", "2024-01-14 12:00"),
        # H2: Surgical ICU, then the Cardiac ICU on the same day, leaving at exactly midnight Feb 2
        ("H2", "icu", "surgical_icu", "2024-01-31 06:00", "2024-01-31 18:00"),
        ("H2", "icu", "cardiac_icu", "2024-01-31 18:00", "2024-02-02 00:00"),
        ("H2", "ward", None, "2024-02-02 00:00", "2024-02-04 12:00"),
        # H3: a child in an ICU; not counted
        ("H3", "icu", "medical_icu", "2024-01-10 10:00", "2024-01-12 12:00"),
        # H4: an ICU row with no out time, which ends at discharge (Mar 5, 6)
        ("H4", "icu", "medical_icu", "2024-03-05 10:00", None),
        # H5: ward only
        ("H5", "ward", None, "2024-04-01 09:00", "2024-04-03 12:00"),
    ]
    adt = pd.DataFrame(adt_rows, columns=["hospitalization_id", "location_category", "location_type", "in_dttm", "out_dttm"])
    adt["in_dttm"] = adt["in_dttm"].map(local)
    adt["out_dttm"] = adt["out_dttm"].map(lambda t: local(t) if t else pd.NaT)
    adt["hospital_id"] = "HOSP-A"
    adt["hospital_type"] = "academic"
    adt["location_name"] = adt["location_type"].fillna(adt["location_category"])
    doses = [
        # H1, Jan 10: cefepime twice and IV vancomycin -> 2 days of therapy, 1 day on antibacterials
        ("H1", "cefepime", "iv", "given", "administered", "2024-01-10 14:00"),
        ("H1", "cefepime", "iv", "given", "administered", "2024-01-10 22:00"),
        ("H1", "vancomycin", "iv", "given", "administered", "2024-01-10 15:00"),
        # H1, Jan 11: meropenem not given; oral vancomycin (all antibacterials, not anti-MRSA);
        # cefepime at 19:00 local, which is Jan 12 in UTC
        ("H1", "meropenem", "iv", "not_given", "not_administered", "2024-01-11 10:00"),
        ("H1", "vancomycin", "enteral", "given", "administered", "2024-01-11 10:00"),
        ("H1", "cefepime", "iv", "given", "administered", "2024-01-11 19:00"),
        # H1, Jan 12: piperacillin/tazobactam as a bolus before leaving the ICU
        ("H1", "piperacillin_tazobactam", "iv", "bolus", "administered", "2024-01-12 08:30"),
        # H1, Jan 13 on the ward, and an analgesic: neither counted
        ("H1", "ceftriaxone", "iv", "given", "administered", "2024-01-13 10:00"),
        ("H1", "acetaminophen", "enteral", "given", "administered", "2024-01-10 16:00"),
        # H2: ceftriaxone in each ICU on Jan 31; cefazolin and fluconazole Feb 1; cefazolin on the ward Feb 2
        ("H2", "ceftriaxone", "iv", "given", "administered", "2024-01-31 10:00"),
        ("H2", "ceftriaxone", "iv", "given", "administered", "2024-01-31 20:00"),
        ("H2", "cefazolin", "iv", "given", "administered", "2024-02-01 10:00"),
        ("H2", "Fluconazole", "iv", "given", "administered", "2024-02-01 11:00"),
        ("H2", "cefazolin", "iv", "given", "administered", "2024-02-02 01:00"),
        # H3: a child
        ("H3", "cefepime", "iv", "given", "administered", "2024-01-10 14:00"),
        # H4: oral linezolid (anti-MRSA by any route) on Mar 6
        ("H4", "linezolid", "enteral", "given", "administered", "2024-03-06 08:00"),
    ]
    meds = pd.DataFrame(doses, columns=["hospitalization_id", "med_category", "med_route_category",
                                        "mar_action_category", "mar_action_group", "admin_dttm"])
    meds["admin_dttm"] = meds["admin_dttm"].map(local)
    meds["med_group"] = "CMS_sepsis_qualifying_antibiotics"
    for name, frame in [("patient", patients), ("hospitalization", hospitalizations), ("adt", adt),
                        ("medication_admin_intermittent", meds)]:
        frame.to_parquet(folder / f"clif_{name}.parquet", index=False)
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"), "dashboards": ["stewardship"],
            "encounters_per_chunk": 2, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


@pytest.fixture(scope="module")
def stay_months(tmp_path_factory):
    folder = tmp_path_factory.mktemp("stewardship_site")
    output = build(load_config(write_site(folder)), STEWARDSHIP)
    rows = pd.read_parquet(output / "stay_months.parquet")
    return rows.set_index(["label_id", "unit", "month"])


COLUMNS = ["days_present", "days_any", "dot_all_antibacterial", "dot_broad_hospital_onset", "dot_broad_community",
           "dot_resistant_gram_positive", "dot_narrow_beta_lactam", "dot_cdi_risk", "dot_antifungal_candidiasis"]
EXPECTED = {
    ("H1", "Medical ICU", "2024-01"): [3, 3, 5, 3, 0, 1, 0, 2, 0],
    ("H2", "Surgical ICU", "2024-01"): [1, 1, 1, 0, 1, 0, 0, 1, 0],
    ("H2", "Cardiac ICU", "2024-01"): [1, 1, 1, 0, 1, 0, 0, 1, 0],
    ("H2", "Cardiac ICU", "2024-02"): [1, 1, 1, 0, 0, 0, 1, 0, 1],
    ("H4", "Medical ICU", "2024-03"): [2, 1, 1, 0, 0, 1, 0, 0, 0],
}


def test_only_adult_icu_time_makes_rows(stay_months):
    assert set(stay_months.index) == set(EXPECTED)


@pytest.mark.parametrize("key", list(EXPECTED))
def test_hand_counted_days_and_days_of_therapy(stay_months, key):
    assert stay_months.loc[key, COLUMNS].astype(int).tolist() == EXPECTED[key]
