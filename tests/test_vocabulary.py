"""CLIF 3.0 category values are read back as their CLIF 2.1 values, and 2.1 values are left alone."""
from __future__ import annotations

from pathlib import Path

import clifpy
import pandas as pd
import pytest
import yaml
from clifpy.utils.crosswalk import load_crosswalk, normalize_category_value

from clif_dash.icu import outlier_bounds
from clif_dash.vocabulary import to_clif21

TABLES = ["patient", "hospitalization", "adt", "respiratory_support", "vitals", "labs", "patient_assessments",
          "medication_admin_continuous", "medication_admin_intermittent", "position", "code_status",
          "microbiology_culture", "microbiology_susceptibility", "crrt_therapy"]
SCHEMAS = Path(clifpy.__file__).parent / "schemas" / "2.1"


@pytest.mark.parametrize("table", TABLES)
def test_every_21_value_survives_clifpys_crosswalk_and_back(table):
    renames = load_crosswalk().get("renames", {}).get(table, {})
    for column in yaml.safe_load((SCHEMAS / f"{table}_schema.yaml").read_text())["columns"]:
        values = [v for v in column.get("permissible_values") or [] if isinstance(v, str)]
        if not values:
            continue
        clif30 = [renames.get(column["name"], {}).get(v) or normalize_category_value(v) for v in values]
        unique = {new for new in clif30 if clif30.count(new) == 1}   # 2.1 values merged in 3.0 cannot come back
        back = to_clif21(table, pd.DataFrame({column["name"]: clif30}))[column["name"]].tolist()
        assert [b for b, new in zip(back, clif30) if new in unique] == \
               [v for v, new in zip(values, clif30) if new in unique], (table, column["name"])
        assert to_clif21(table, pd.DataFrame({column["name"]: values}))[column["name"]].tolist() == values


def test_the_values_the_respiratory_code_names():
    frame = pd.DataFrame({"device_category": ["imv", "trach_collar", "hfnc", "IMV"],
                          "mode_category": ["acvc", "ps_or_cpap", "t_piece", None]})
    assert to_clif21("respiratory_support", frame).to_dict("list") == {
        "device_category": ["IMV", "Trach Collar", "High Flow NC", "IMV"],
        "mode_category": ["Assist Control-Volume Control", "Pressure Support/CPAP", "Blow by", None]}
    assert to_clif21("hospitalization", pd.DataFrame({"discharge_category": ["expired", "snf"]}))[
        "discharge_category"].tolist() == ["Expired", "Skilled Nursing Facility (SNF)"]
    assert to_clif21("code_status", pd.DataFrame({"code_status_category": ["dnr_or_dni", "presume_full"]}))[
        "code_status_category"].tolist() == ["DNR/DNI", "Presume Full"]


def test_outlier_ranges_accept_either_version():
    assert outlier_bounds("labs", "lab_value_numeric", "glucose") == \
        outlier_bounds("labs", "lab_value_numeric", "glucose_fingerstick") == (0.0, 2000.0)
    assert outlier_bounds("patient_assessments", "numerical_value", "rass") == (-5.0, 4.0)
