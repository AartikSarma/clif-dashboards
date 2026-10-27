"""Fluids: input volume per ICU day by CLIF 3.0 input group, by ICU and month."""
from __future__ import annotations

from ...dashboard import Dashboard, Metric, RowTable
from . import build

STAY_MONTHS = RowTable("stay_months", "row_id", "ICU stay-month", "ICU stay-months")
MEASURES = {"total_input": "vol_total", "iv_fluids": "vol_iv_fluids", "iv_crystalloid": "vol_iv_crystalloid",
            "iv_colloid": "vol_iv_colloid", "blood_products": "vol_blood_products", "nutrition": "vol_nutrition",
            "medication_fluids": "vol_medication_fluids", "renal_replacement_fluids": "vol_renal_replacement_fluids",
            "flush_irrigation": "vol_flush_irrigation", "other_input": "vol_other"}


def _any(column):
    return lambda rows, metric: rows[column].fillna(0) > 0


METRICS = [Metric(measure, "stay_months", "rate", column, "icu_days", "ICU days", scale=1,
                  missed=_any(column), missed_label="with any")
           for measure, column in MEASURES.items()]

FLUIDS = Dashboard(
    id="fluids",
    title="ICU Fluids",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(STAY_MONTHS,),
    metrics=METRICS,
    domains=["All input", "IV fluids", "Other input"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    # Volumes cluster within patients, so no measure gets control limits
    independent_denominators=frozenset(),
)
