"""The microbiology build on a hand-made CLIF site, with counts worked out by hand.

There is no reference implementation, and the synthetic data cannot exercise most
of these rules (it has no common commensals, draws its blood cultures after the
antibiotics start, and tests for C. difficile only on the first hospital day), so
each encounter here is built to test particular rules. Times are local
(America/Los_Angeles) and stored in UTC.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from clif_dash.build import build
from clif_dash.config import load_config
from clif_dash.dashboards import MICROBIOLOGY

REPO = Path(__file__).resolve().parent.parent
TZ = "America/Los_Angeles"


def local(text: str | None):
    return pd.Timestamp(text, tz=TZ).tz_convert("UTC") if text else pd.NaT


def write_site(folder: Path) -> Path:
    patients = pd.DataFrame({"patient_id": ["P1", "P2", "P3"], "sex_category": ["Female", "Male", "Female"],
                             "race_category": ["White"] * 3, "ethnicity_category": ["Non-Hispanic"] * 3})
    hospitalizations = pd.DataFrame({
        "patient_id": ["P1", "P2", "P3", "P2"],
        "hospitalization_id": ["H1", "H2", "H3", "H4"],
        "admission_dttm": [local("2024-03-01 08:00"), local("2024-03-01 08:00"), local("2024-03-01 08:00"),
                           local("2024-04-10 08:00")],
        "discharge_dttm": [local("2024-03-12 12:00"), local("2024-03-08 12:00"), local("2024-03-05 12:00"),
                           local("2024-04-16 12:00")],
        "age_at_admission": [60, 70, 10, 70],
        "admission_type_category": ["ed"] * 4,
        "discharge_category": ["Home"] * 4,
    })
    adt = pd.DataFrame([
        ("H1", "icu", "medical_icu", "2024-03-01 10:00", "2024-03-10 10:00"),   # Mar 1-10: 10 days
        ("H2", "ward", None, "2024-03-01 08:00", "2024-03-03 08:00"),
        ("H2", "icu", "surgical_icu", "2024-03-03 08:00", "2024-03-06 08:00"),  # Mar 3-6: 4 days
        ("H3", "icu", "medical_icu", "2024-03-01 08:00", "2024-03-05 12:00"),   # a child
        ("H4", "icu", "medical_icu", "2024-04-10 10:00", "2024-04-14 10:00"),   # Apr 10-14: 5 days
    ], columns=["hospitalization_id", "location_category", "location_type", "in_dttm", "out_dttm"])
    adt["in_dttm"], adt["out_dttm"] = adt["in_dttm"].map(local), adt["out_dttm"].map(local)
    adt["hospital_id"], adt["hospital_type"] = "HOSP-A", "academic"
    adt["location_name"] = adt["location_type"].fillna(adt["location_category"])

    cultures = [
        # H1 blood: a lone coagulase-negative staphylococcus on day 2 is a contaminant
        ("H1", "B1", "2024-03-02 09:00", "blood_buffy", "staphylococcus_epidermidis", "staphylococcus_coag_neg"),
        # no growth on day 3
        ("H1", "B2", "2024-03-03 09:00", "blood_buffy", "no_growth", "no_growth"),
        # the same commensal in two draws on day 5 is real: both positive, one hospital-onset event
        ("H1", "B3", "2024-03-05 09:00", "blood_buffy", "staphylococcus_epidermidis", "staphylococcus_coag_neg"),
        ("H1", "B4", "2024-03-05 21:00", "blood_buffy", "staphylococcus_epidermidis", "staphylococcus_coag_neg"),
        # E. coli on day 7 falls inside the 14-day repeat window
        ("H1", "B5", "2024-03-07 09:00", "blood_buffy", "escherichia_coli", "escherichia"),
        # one urine draw growing three organisms
        ("H1", "U1", "2024-03-04 09:00", "genito_urinary_tract", "escherichia_coli", "escherichia"),
        ("H1", "U2", "2024-03-04 09:00", "genito_urinary_tract", "klebsiella_pneumoniae", "klebsiella"),
        ("H1", "U3", "2024-03-04 09:00", "genito_urinary_tract", "pseudomonas_aeruginosa", "pseudomonas_wo_cepacia_maltophilia"),
        # H2 blood: S. aureus on day 3 is not hospital-onset
        ("H2", "B6", "2024-03-03 12:00", "blood_buffy", "staphylococcus_aureus", "staphylococcus_coag_pos"),
        # H3 is a child
        ("H3", "B7", "2024-03-02 09:00", "blood_buffy", "escherichia_coli", "escherichia"),
    ]
    culture = pd.DataFrame(cultures, columns=["hospitalization_id", "organism_id", "collect_dttm", "fluid_category",
                                              "organism_category", "organism_group"])
    culture["collect_dttm"] = culture["collect_dttm"].map(local)
    culture["order_dttm"] = culture["collect_dttm"]
    culture["result_dttm"] = culture["collect_dttm"] + pd.Timedelta(days=2)
    culture["method_category"] = "culture"
    culture["patient_id"] = culture["hospitalization_id"].map({"H1": "P1", "H2": "P2", "H3": "P3", "H4": "P2"})

    susceptibility = pd.DataFrame([
        ("U1", "ceftriaxone", "non_susceptible"), ("U1", "meropenem", "susceptible"),
        ("U2", "ceftriaxone", "susceptible"), ("U2", "meropenem", "non_susceptible"),
        ("U2", "ertapenem", "non_susceptible"),
        # ertapenem is ignored for Pseudomonas, so this isolate is carbapenem-susceptible
        ("U3", "ertapenem", "non_susceptible"), ("U3", "meropenem", "susceptible"),
        ("B6", "oxacillin", "non_susceptible"),
        ("B5", "ceftriaxone", "susceptible"),      # a later E. coli: not the first isolate
    ], columns=["organism_id", "antimicrobial_category", "susceptibility_category"])

    nonculture = pd.DataFrame([
        ("H1", "2024-03-02 09:00"),   # day 2: community-onset
        ("H1", "2024-03-09 09:00"),   # day 9, but 7 days after the patient's last positive here: a duplicate
        ("H2", "2024-03-04 09:00"),   # day 4 in the ICU: hospital-onset, incident
        ("H2", "2024-03-05 09:00"),   # a duplicate
        ("H4", "2024-04-13 09:00"),   # day 4, but 40 days after the patient's last event: recurrent
    ], columns=["hospitalization_id", "collect_dttm"])
    nonculture["collect_dttm"] = nonculture["collect_dttm"].map(local)
    nonculture["organism_category"], nonculture["result_category"] = "clostridium_difficile", "detected"
    nonculture["method_category"] = "pcr"

    doses = [
        # H1: cefepime on day 6 is a new start, 23 h after a blood culture; the evening dose is not new;
        # the day 9 IV dose follows an oral vancomycin dose 10 h earlier, so it is not new either
        ("H1", "cefepime", "iv", "2024-03-06 08:00"), ("H1", "cefepime", "iv", "2024-03-06 16:00"),
        ("H1", "vancomycin", "enteral", "2024-03-09 10:00"), ("H1", "cefepime", "iv", "2024-03-09 20:00"),
        # H2: a start on the ward (not counted), a continuation in the ICU, and a new ICU start on day 6
        # 67 h after the only blood culture
        ("H2", "piperacillin_tazobactam", "iv", "2024-03-02 10:00"),
        ("H2", "piperacillin_tazobactam", "iv", "2024-03-03 10:00"),
        ("H2", "meropenem", "iv", "2024-03-06 07:00"),
    ]
    meds = pd.DataFrame(doses, columns=["hospitalization_id", "med_category", "med_route_category", "admin_dttm"])
    meds["admin_dttm"] = meds["admin_dttm"].map(local)
    meds["mar_action_category"], meds["mar_action_group"] = "given", "administered"
    meds["med_group"] = "CMS_sepsis_qualifying_antibiotics"

    for name, frame in [("patient", patients), ("hospitalization", hospitalizations), ("adt", adt),
                        ("microbiology_culture", culture), ("microbiology_susceptibility", susceptibility),
                        ("microbiology_nonculture", nonculture), ("medication_admin_intermittent", meds)]:
        frame.to_parquet(folder / f"clif_{name}.parquet", index=False)
    decisions = yaml.safe_load((REPO / "config" / "decisions" / "microbiology.yaml").read_text())
    for decision in decisions["shared"]:
        if decision["id"] == "culture_coverage":
            decision["value"] = "all"
    (folder / "microbiology_decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True))
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"), "dashboards": ["microbiology"],
            "decisions": {"microbiology": str(folder / "microbiology_decisions.yaml")}, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


@pytest.fixture(scope="module")
def built_site(tmp_path_factory):
    output = build(load_config(write_site(tmp_path_factory.mktemp("microbiology_site"))), MICROBIOLOGY)
    return {name: pd.read_parquet(output / f"{name}.parquet") for name in ("stay_months", "antibiotic_starts", "isolates")}


COUNTS = ["days_present", "blood_draws", "contaminated_draws", "urine_draws", "hob_events", "cdi_events"]
EXPECTED = {
    ("H1", "Medical ICU", "2024-03"): [10, 5, 1, 1, 1, 0],
    ("H2", "Surgical ICU", "2024-03"): [4, 1, 0, 0, 0, 1],
    ("H4", "Medical ICU", "2024-04"): [5, 0, 0, 0, 0, 0],
}


def test_stay_months(built_site):
    rows = built_site["stay_months"].set_index(["label_id", "unit", "month"])
    assert set(rows.index) == set(EXPECTED)          # the child (H3) makes no row
    for key, expected in EXPECTED.items():
        assert rows.loc[key, COUNTS].astype(int).tolist() == expected, key


def test_new_antibacterial_starts_and_prior_cultures(built_site):
    starts = built_site["antibiotic_starts"].set_index("label_id")
    assert sorted(starts.index) == ["H1", "H2"]
    assert starts.loc["H1", "cultured"] == 1 and starts.loc["H2", "cultured"] == 0


def test_first_isolates_and_resistance(built_site):
    isolates = built_site["isolates"].set_index(["label_id", "organism"])
    columns = ["mrsa_num", "mrsa_den", "ctx_num", "ctx_den", "carb_num", "carb_den"]
    expected = {
        ("H1", "escherichia_coli"): [0, 0, 1, 1, 0, 1],
        ("H1", "klebsiella_pneumoniae"): [0, 0, 0, 1, 1, 1],
        ("H1", "pseudomonas_aeruginosa"): [0, 0, 0, 0, 0, 1],
        ("H2", "staphylococcus_aureus"): [1, 1, 0, 0, 0, 0],
    }
    assert set(isolates.index) == set(expected)      # the commensal has no tested drug and is left out
    for key, values in expected.items():
        assert isolates.loc[key, columns].astype(int).tolist() == values, key


def test_the_antibacterial_list_matches_stewardship():
    def agents(name, measure):
        decisions = yaml.safe_load((REPO / "config" / "decisions" / f"{name}.yaml").read_text())
        return next(d["value"] for d in decisions["metrics"][measure]["decisions"] if d["id"] == "agents")
    assert agents("microbiology", "cultures_before_antibiotics") == agents("stewardship", "all_antibacterial")
