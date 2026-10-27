"""The stewardship build on a hand-made CLIF site with counts worked out by hand.

There is no upstream for this dashboard, so a few encounters stand in for an
oracle, each built to test one counting rule. Times are written in the site's
local time (America/Los_Angeles, UTC-8 in winter) and stored in UTC, as CLIF does.
"""
from __future__ import annotations

import json
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


def write_site(folder: Path, coverage: str = "all") -> Path:
    """The hand-made site; `coverage` sets the medication_coverage decision."""
    patients = pd.DataFrame({
        "patient_id": ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
        "sex_category": ["Female", "Male", "Female", "Male", "Female", "Male", "Female"],
        "race_category": ["White"] * 7, "ethnicity_category": ["Non-Hispanic"] * 7,
    })
    hospitalizations = pd.DataFrame({
        "patient_id": ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
        "hospitalization_id": ["H1", "H2", "H3", "H4", "H5", "H6", "H7"],
        "admission_dttm": [local("2024-01-10 10:00"), local("2024-01-31 05:00"), local("2024-01-10 10:00"),
                           local("2024-03-05 09:00"), local("2024-04-01 09:00"), local("2024-05-01 09:00"),
                           local("2024-06-01 09:00")],
        "discharge_dttm": [local("2024-01-14 12:00"), local("2024-02-04 12:00"), local("2024-01-12 12:00"),
                           local("2024-03-06 12:00"), local("2024-04-03 12:00"), local("2024-05-06 12:00"),
                           local("2024-06-08 12:00")],
        "age_at_admission": [60, 70, 10, 50, 40, 55, 65],
        "admission_type_category": ["ed"] * 7,
        "discharge_category": ["Home"] * 7,
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
        # H6: Medical ICU at hospital A, then transferred to a Medical ICU at hospital B in the same month
        ("H6", "icu", "medical_icu", "2024-05-01 10:00", "2024-05-02 10:00"),
        ("H6", "icu", "medical_icu", "2024-05-03 10:00", "2024-05-05 10:00"),
        # H7: Medical ICU June 1 to June 6, for duplicative anaerobic coverage
        ("H7", "icu", "medical_icu", "2024-06-01 10:00", "2024-06-06 10:00"),
    ]
    adt = pd.DataFrame(adt_rows, columns=["hospitalization_id", "location_category", "location_type", "in_dttm", "out_dttm"])
    adt["in_dttm"] = adt["in_dttm"].map(local)
    adt["out_dttm"] = adt["out_dttm"].map(lambda t: local(t) if t else pd.NaT)
    adt["hospital_id"] = "HOSP-A"
    adt.loc[(adt["hospitalization_id"] == "H6") & (adt["in_dttm"] > local("2024-05-02 12:00")), "hospital_id"] = "HOSP-B"
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
        # H6: meropenem at each hospital
        ("H6", "meropenem", "iv", "given", "administered", "2024-05-01 12:00"),
        ("H6", "meropenem", "iv", "given", "administered", "2024-05-04 12:00"),
        # H7: metronidazole with piperacillin/tazobactam June 1 and 2 (two days running: both duplicative);
        # oral metronidazole with it June 3 (oral metronidazole does not count toward two); a switch
        # from piperacillin/tazobactam to meropenem June 4 (two agents, but no neighbour day with two);
        # meropenem alone June 5
        ("H7", "metronidazole", "iv", "given", "administered", "2024-06-01 12:00"),
        ("H7", "piperacillin_tazobactam", "iv", "given", "administered", "2024-06-01 13:00"),
        ("H7", "metronidazole", "iv", "given", "administered", "2024-06-02 12:00"),
        ("H7", "piperacillin_tazobactam", "iv", "given", "administered", "2024-06-02 13:00"),
        ("H7", "metronidazole", "enteral", "given", "administered", "2024-06-03 12:00"),
        ("H7", "piperacillin_tazobactam", "iv", "given", "administered", "2024-06-03 13:00"),
        ("H7", "piperacillin_tazobactam", "iv", "given", "administered", "2024-06-04 08:00"),
        ("H7", "meropenem", "iv", "given", "administered", "2024-06-04 20:00"),
        ("H7", "meropenem", "iv", "given", "administered", "2024-06-05 08:00"),
    ]
    meds = pd.DataFrame(doses, columns=["hospitalization_id", "med_category", "med_route_category",
                                        "mar_action_category", "mar_action_group", "admin_dttm"])
    meds["admin_dttm"] = meds["admin_dttm"].map(local)
    meds["med_group"] = "CMS_sepsis_qualifying_antibiotics"
    for name, frame in [("patient", patients), ("hospitalization", hospitalizations), ("adt", adt),
                        ("medication_admin_intermittent", meds)]:
        frame.to_parquet(folder / f"clif_{name}.parquet", index=False)
    decisions = yaml.safe_load((REPO / "config" / "decisions" / "stewardship.yaml").read_text())
    for decision in decisions["shared"]:
        if decision["id"] == "medication_coverage":
            decision["value"] = coverage
    (folder / "stewardship_decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True))
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"), "dashboards": ["stewardship"],
            "decisions": {"stewardship": str(folder / "stewardship_decisions.yaml")},
            "encounters_per_chunk": 2, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


@pytest.fixture(scope="module")
def stay_months(tmp_path_factory):
    folder = tmp_path_factory.mktemp("stewardship_site")
    output = build(load_config(write_site(folder)), STEWARDSHIP)
    rows = pd.read_parquet(output / "stay_months.parquet")
    return rows.set_index(["label_id", "hospital", "unit", "month"])


COLUMNS = ["days_present", "days_any", "dot_all_antibacterial", "dot_broad_hospital_onset", "dot_broad_community",
           "dot_resistant_gram_positive", "dot_narrow_beta_lactam", "dot_cdi_risk", "dot_antifungal_candidiasis",
           "dot_anaerobic", "days_anaerobic", "days_redundant_anaerobic"]
EXPECTED = {
    ("H1", "HOSP-A", "Medical ICU", "2024-01"): [3, 3, 5, 3, 0, 1, 0, 2, 0, 1, 1, 0],
    ("H2", "HOSP-A", "Surgical ICU", "2024-01"): [1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0],
    ("H2", "HOSP-A", "Cardiac ICU", "2024-01"): [1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0],
    ("H2", "HOSP-A", "Cardiac ICU", "2024-02"): [1, 1, 1, 0, 0, 0, 1, 0, 1, 0, 0, 0],
    ("H4", "HOSP-A", "Medical ICU", "2024-03"): [2, 1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0],
    ("H6", "HOSP-A", "Medical ICU", "2024-05"): [2, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 0],
    ("H6", "HOSP-B", "Medical ICU", "2024-05"): [3, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 0],
    # H7: anaerobic DOT 2+2+2+2+1; five days on anti-anaerobic agents; June 1 and 2 duplicative
    ("H7", "HOSP-A", "Medical ICU", "2024-06"): [6, 5, 9, 6, 0, 0, 0, 0, 0, 9, 5, 2],
}


def test_only_adult_icu_time_makes_rows(stay_months):
    assert set(stay_months.index) == set(EXPECTED)


def test_rows_have_unique_ids_even_across_hospitals(stay_months):
    assert stay_months["row_id"].is_unique


@pytest.mark.parametrize("key", list(EXPECTED))
def test_hand_counted_days_and_days_of_therapy(stay_months, key):
    assert stay_months.loc[key, COLUMNS].astype(int).tolist() == EXPECTED[key]


def test_only_whole_months_inside_the_medication_records_count(tmp_path):
    """The first dose is Jan 10 and the last admission June 1, so February to May count:
    January's rows (H1, H2) and June's (H7) are left out, and the rest are unchanged."""
    output = build(load_config(write_site(tmp_path, coverage="whole_months")), STEWARDSHIP)
    rows = pd.read_parquet(output / "stay_months.parquet").set_index(["label_id", "hospital", "unit", "month"])
    kept = [key for key in EXPECTED if "2024-02" <= key[3] <= "2024-05"]
    assert set(rows.index) == set(kept)
    for key in kept:
        assert rows.loc[key, COLUMNS].astype(int).tolist() == EXPECTED[key]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["data_quality"]["months_counted"] == {"first": "2024-02", "last": "2024-05", "icu_days_left_out": 11}
