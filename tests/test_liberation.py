"""The ICU Liberation build on a hand-made site, with each day's counts worked out by hand.

The synthetic data has no documented SAT or SBT, no CPOT or BPS and no
benzodiazepine or paralytic infusions, so this site is the only test of those
rules. Times are local (America/Los_Angeles, UTC-8 in winter) and stored in UTC.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from clif_dash.build import build
from clif_dash.config import load_config
from clif_dash.dashboards import LIBERATION
from clif_dash.server.app import create_app
from clif_dash.upstream import prep

REPO = Path(__file__).resolve().parent.parent
TZ = "America/Los_Angeles"


def local(text: str):
    return pd.Timestamp(text, tz=TZ).tz_convert("UTC")


def write_site(folder: Path) -> Path:
    pd.DataFrame({"patient_id": ["P1", "P2"], "sex_category": ["Female"] * 2, "race_category": ["White"] * 2,
                  "ethnicity_category": ["Non-Hispanic"] * 2}).to_parquet(folder / "clif_patient.parquet", index=False)
    pd.DataFrame({
        "patient_id": ["P1", "P2"], "hospitalization_id": ["H1", "H2"],
        "admission_dttm": [local("2024-01-10 08:00")] * 2, "discharge_dttm": [local("2024-01-14 12:00")] * 2,
        "age_at_admission": [60, 10], "admission_type_category": ["ed"] * 2, "discharge_category": ["Home"] * 2,
    }).to_parquet(folder / "clif_hospitalization.parquet", index=False)
    adt = pd.DataFrame([("H1", "2024-01-10 12:00", "2024-01-12 09:00"),    # Jan 10, 11, 12
                        ("H2", "2024-01-10 12:00", "2024-01-12 09:00")],   # a child
                       columns=["hospitalization_id", "in_dttm", "out_dttm"])
    adt["in_dttm"], adt["out_dttm"] = adt["in_dttm"].map(local), adt["out_dttm"].map(local)
    adt["location_category"], adt["location_type"], adt["location_name"] = "icu", "medical_icu", "MICU"
    adt["hospital_id"], adt["hospital_type"] = "HOSP-A", "academic"
    adt.to_parquet(folder / "clif_adt.parquet", index=False)

    assessments = pd.DataFrame([
        # Day 10: pain scored; RASS 0, -4, -4 (deep on most readings); CAM positive; an SAT given (failed);
        # an SBT recorded as not performed
        ("H1", "2024-01-10 13:00", "NRS", 3, None),
        ("H1", "2024-01-10 13:00", "RASS", 0, None),
        ("H1", "2024-01-10 18:00", "RASS", -4, None),
        ("H1", "2024-01-10 20:00", "RASS", -4, None),
        ("H1", "2024-01-10 14:00", "cam_total", 1, "Positive"),
        ("H1", "2024-01-10 15:00", "sat_delivery_pass_fail", None, "Fail"),
        ("H1", "2024-01-10 16:00", "sbt_delivery_performed", None, "No"),
        # Day 11: RASS -5 during a paralytic infusion (assessed, but not a scored reading); RASS +1;
        # CAM unable to assess; CPOT; an SBT passed
        ("H1", "2024-01-11 02:00", "RASS", -5, None),
        ("H1", "2024-01-11 10:00", "RASS", 1, None),
        ("H1", "2024-01-11 11:00", "cam_total", None, "Unable to Assess"),
        ("H1", "2024-01-11 12:00", "cpot_total", 2, None),
        ("H1", "2024-01-11 13:00", "sbt_delivery_pass_fail", None, "Pass"),
        # Day 12: a CAM recorded only as the number 0, a negative screen
        ("H1", "2024-01-12 08:00", "cam_total", 0, None),
        ("H1", "2024-01-12 10:00", "RASS", 0, None),                      # after the ICU: not counted
        ("H2", "2024-01-10 13:00", "RASS", 0, None),                      # a child
    ], columns=["hospitalization_id", "recorded_dttm", "assessment_category", "numerical_value", "categorical_value"])
    assessments["recorded_dttm"] = assessments["recorded_dttm"].map(local)
    assessments["numerical_value"] = assessments["numerical_value"].astype(float)
    assessments.to_parquet(folder / "clif_patient_assessments.parquet", index=False)

    infusions = pd.DataFrame([
        # Propofol from before the ICU to Jan 11 06:00: sedative days 10 and 11. In the ICU from 12:00 at
        # 20 mcg/kg/min, raised to 40 at 22:00
        ("H1", "2024-01-10 10:00", "propofol", 20.0, "mcg/kg/min", "start"),
        ("H1", "2024-01-10 22:00", "propofol", 40.0, "mcg/kg/min", "dose_change"),
        ("H1", "2024-01-11 06:00", "propofol", 40.0, "mcg/kg/min", "stop"),
        # Midazolam charted once at 20:00 on Jan 11 runs 6 h: benzodiazepine (and sedative) days 11 and 12;
        # 2,000 mcg/hr is 2 mg/hr
        ("H1", "2024-01-11 20:00", "midazolam", 2000.0, "mcg/hr", "going"),
        # Cisatracurium from midnight to 04:00 on Jan 11: a paralytic day
        ("H1", "2024-01-11 00:00", "cisatracurium", 1.0, "mg/hr", "start"),
        ("H1", "2024-01-11 04:00", "cisatracurium", 1.0, "mg/hr", "stop"),
        # Fentanyl in a unit the converter does not know: 6 h running, with no rate
        ("H1", "2024-01-11 12:00", "fentanyl", 3.0, "puffs", "going"),
    ], columns=["hospitalization_id", "admin_dttm", "med_category", "med_dose", "med_dose_unit", "mar_action_category"])
    infusions["admin_dttm"] = infusions["admin_dttm"].map(local)
    infusions["mar_action_group"] = "administered"
    infusions.to_parquet(folder / "clif_medication_admin_continuous.parquet", index=False)

    support = pd.DataFrame([("H1", "2024-01-10 14:00", "IMV"), ("H1", "2024-01-11 08:00", "IMV"),
                            ("H1", "2024-01-12 08:00", "Nasal Cannula")],
                           columns=["hospitalization_id", "recorded_dttm", "device_category"])
    support["recorded_dttm"] = support["recorded_dttm"].map(local)
    support.to_parquet(folder / "clif_respiratory_support.parquet", index=False)

    decisions = yaml.safe_load((REPO / "config" / "decisions" / "liberation.yaml").read_text())
    for decision in decisions["shared"]:
        if decision["id"] == "assessment_coverage":
            decision["value"] = "all"
    (folder / "liberation_decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True))
    site = {"clif_dir": str(folder), "output_dir": str(folder / "output"), "site_timezone": TZ,
            "decisions_dir": str(REPO / "config" / "decisions"),
            "decisions": {"liberation": str(folder / "liberation_decisions.yaml")}, "workers": 1}
    (folder / "site.yaml").write_text(yaml.safe_dump(site))
    return folder / "site.yaml"


EXPECTED = {
    "days_present": 3, "pain_days": 2, "rass_days": 2, "rass_readings": 4, "rass_in_range": 2, "rass_scored_days": 2,
    "deep_days": 1, "cam_days": 3, "cam_result_days": 2, "cam_positive_days": 1, "sedative_days": 3,
    "benzodiazepine_days": 2, "imv_days": 2, "sat_eligible_days": 1, "sat_days": 1, "sbt_days": 1,
}


@pytest.fixture(scope="module")
def built_site(tmp_path_factory):
    cfg = load_config(write_site(tmp_path_factory.mktemp("liberation_site")))
    return cfg, build(cfg, LIBERATION)


def test_each_day(built_site):
    _, output = built_site
    rows = pd.read_parquet(output / "stay_months.parquet").set_index("label_id")
    assert list(rows.index) == ["H1"]                         # the child (H2) makes no row
    assert rows.loc["H1", list(EXPECTED)].astype(int).to_dict() == EXPECTED


def test_drug_lists_and_rass_rules_match_respiratory():
    def decisions(name):
        return yaml.safe_load((REPO / "config" / "decisions" / f"{name}.yaml").read_text())
    liberation, respiratory = decisions("liberation"), decisions("respiratory")
    shared = {d["id"]: d["value"] for d in liberation["shared"]}

    def value(source, measure, decision):
        return next(d["value"] for d in source["metrics"][measure]["decisions"] if d["id"] == decision)

    assert shared["sedatives"] == value(respiratory, "sat", "sedatives")
    assert shared["paralytics"] == prep.PARALYTIC_CATS
    assert value(liberation, "light_sedation", "target_range") == value(respiratory, "light_sedation", "target_range")


def test_infusion_hours_and_rates_by_hour_of_day(built_site):
    cfg, output = built_site
    hours = pd.read_parquet(output / "sedation_hours.parquet")
    totals = hours.drop(columns=["row_id", "month", "hospital", "unit", "hour"]).sum()
    assert totals["icu_hours"] == pytest.approx(45)            # Jan 10 12:00 to Jan 12 09:00
    assert totals["propofol_hours"] == pytest.approx(18)       # 10 h at 20, then 8 h at 40
    assert totals["propofol_rate_hours"] == pytest.approx(10 * 20 + 8 * 40)
    assert totals["midazolam_rate_hours"] == pytest.approx(6 * 2)   # converted to mg/hr
    assert totals["fentanyl_hours"] == pytest.approx(6) and totals["fentanyl_rated_hours"] == 0
    assert totals["sedative_hours"] == pytest.approx(24)       # propofol and midazolam never overlap; fentanyl is no sedative

    client = TestClient(create_app(cfg, [LIBERATION]))
    heatmaps = {h["id"]: h for h in client.get("/api/liberation/meta").json()["heatmaps"]}
    assert heatmaps["propofol_rate"]["unit"] == "mcg/kg/min" and heatmaps["midazolam_rate"]["unit"] == "mg/hr"
    propofol = client.get("/api/liberation/heatmap/propofol_rate").json()
    assert propofol["months"] == ["2024-01"]
    assert propofol["values"][12][0] == 20 and propofol["values"][23][0] == 40 and propofol["values"][7][0] is None
    sedated = client.get("/api/liberation/heatmap/sedative_time").json()
    # 03:00: in the ICU on Jan 11 and Jan 12 (2 h), on propofol on Jan 11 only
    assert sedated["values"][3][0] == pytest.approx(50)


def test_rass_by_hour_of_day(built_site):
    cfg, output = built_site
    hours = pd.read_parquet(output / "sedation_hours.parquet")
    # 0, -4 and -4 on Jan 10 and +1 on Jan 11; the -5 during cisatracurium and the reading after the ICU are left out
    assert hours[["rass_readings", "rass_sum", "rass_in_range", "rass_deep"]].sum().tolist() == [4, -7, 2, 2]
    client = TestClient(create_app(cfg, [LIBERATION]))
    mean = client.get("/api/liberation/heatmap/rass_mean").json()
    assert mean["values"][18][0] == -4 and mean["values"][10][0] == 1 and mean["values"][2][0] is None
    assert mean["heatmap"]["floor"] is None
    target = client.get("/api/liberation/heatmap/rass_target").json()
    assert target["values"][13][0] == 100 and target["values"][20][0] == 0
