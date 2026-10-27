"""ICU Liberation (ABCDEF bundle): pain, SAT and SBT, sedation and delirium per ICU day, by ICU."""
from __future__ import annotations

from ...dashboard import Dashboard, Metric, RowTable
from . import build

STAY_MONTHS = RowTable("stay_months", "row_id", "ICU stay-month", "ICU stay-months")
DAYS = "days present"


def _any(column):
    return lambda rows, metric: rows[column].fillna(0) > 0


def _short_of(numerator, denominator):
    """Stay-months where some days counted in the denominator missed the measure."""
    return lambda rows, metric: rows[numerator].fillna(0) < rows[denominator].fillna(0)


METRICS = [
    Metric("pain_assessed", "stay_months", "ratio", "pain_days", "days_present", DAYS,
           missed=_short_of("pain_days", "days_present")),
    Metric("sat", "stay_months", "ratio", "sat_days", "sat_eligible_days", "SAT-eligible days",
           missed=_short_of("sat_days", "sat_eligible_days")),
    Metric("sbt", "stay_months", "ratio", "sbt_days", "imv_days", "days on invasive ventilation",
           missed=_short_of("sbt_days", "imv_days")),
    Metric("sedation_assessed", "stay_months", "ratio", "rass_days", "days_present", DAYS,
           missed=_short_of("rass_days", "days_present")),
    Metric("light_sedation", "stay_months", "ratio", "rass_in_range", "rass_readings", "RASS readings",
           missed=_short_of("rass_in_range", "rass_readings")),
    Metric("deep_sedation", "stay_months", "ratio", "deep_days", "rass_scored_days", "days with a RASS",
           missed=_any("deep_days"), missed_label="with any"),
    Metric("sedative_infusion", "stay_months", "ratio", "sedative_days", "days_present", DAYS,
           missed=_any("sedative_days"), missed_label="with any"),
    Metric("benzodiazepine_infusion", "stay_months", "ratio", "benzodiazepine_days", "days_present", DAYS,
           missed=_any("benzodiazepine_days"), missed_label="with any"),
    Metric("delirium_screened", "stay_months", "ratio", "cam_days", "days_present", DAYS,
           missed=_short_of("cam_days", "days_present")),
    Metric("delirium", "stay_months", "ratio", "cam_positive_days", "cam_result_days", "days with a CAM-ICU result",
           missed=_any("cam_positive_days"), missed_label="with any"),
]

LIBERATION = Dashboard(
    id="liberation",
    title="ICU Liberation",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(STAY_MONTHS,),
    metrics=METRICS,
    domains=["A: Pain", "B: Awakening and breathing trials", "C: Sedation", "D: Delirium"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    # ICU days and RASS readings cluster within patients, so no measure gets control limits
    independent_denominators=frozenset(),
)
