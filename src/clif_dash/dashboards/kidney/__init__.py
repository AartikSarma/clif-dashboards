"""Kidney: acute kidney injury arising in the ICU, creatinine monitoring and CRRT, by ICU."""
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
    Metric("aki_icu_onset", "stay_months", "rate", "aki_events", "days_present", DAYS, scale=1000,
           missed=_any("aki_events"), missed_label="with an onset"),
    Metric("severe_aki_icu_onset", "stay_months", "rate", "severe_aki_events", "days_present", DAYS, scale=1000,
           missed=_any("severe_aki_events"), missed_label="with an onset"),
    Metric("creatinine_measured", "stay_months", "ratio", "creatinine_days", "days_present", DAYS,
           missed=_short_of("creatinine_days", "days_present")),
    Metric("crrt_use", "stay_months", "ratio", "crrt_days", "days_present", DAYS,
           missed=_any("crrt_days"), missed_label="with any"),
    Metric("crrt_dose", "stay_months", "ratio", "crrt_dose_target_days", "crrt_dose_days",
           "CRRT days with a dose", missed=_short_of("crrt_dose_target_days", "crrt_dose_days")),
]

KIDNEY = Dashboard(
    id="kidney",
    title="ICU Kidney Injury and CRRT",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(STAY_MONTHS,),
    metrics=METRICS,
    domains=["Acute kidney injury", "CRRT"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    # ICU days cluster within patients, so no measure gets control limits
    independent_denominators=frozenset(),
)
