"""Glycemic control: hypo- and hyperglycemia per ICU day with a glucose result, and insulin infusions, by ICU."""
from __future__ import annotations

from ...dashboard import Dashboard, Metric, RowTable
from . import build

STAY_MONTHS = RowTable("stay_months", "row_id", "ICU stay-month", "ICU stay-months")
GLUCOSE_DAYS = "days with a glucose result"


def _any(column):
    return lambda rows, metric: rows[column].fillna(0) > 0


def _short_of(numerator, denominator):
    """Stay-months where some days counted in the denominator missed the measure."""
    return lambda rows, metric: rows[numerator].fillna(0) < rows[denominator].fillna(0)


METRICS = [
    Metric("severe_hypoglycemia", "stay_months", "ratio", "days_severe_hypo", "glucose_days", GLUCOSE_DAYS,
           missed=_any("days_severe_hypo"), missed_label="with any"),
    Metric("hypoglycemia", "stay_months", "ratio", "days_hypo", "glucose_days", GLUCOSE_DAYS,
           missed=_any("days_hypo"), missed_label="with any"),
    Metric("hypoglycemia_on_insulin", "stay_months", "ratio", "insulin_hypo_days", "insulin_glucose_days",
           "insulin infusion days with a glucose result", missed=_any("insulin_hypo_days"), missed_label="with any"),
    Metric("severe_hyperglycemia", "stay_months", "ratio", "days_severe_hyper", "glucose_days", GLUCOSE_DAYS,
           missed=_any("days_severe_hyper"), missed_label="with any"),
    Metric("glucose_in_range", "stay_months", "ratio", "days_in_range", "glucose_days", GLUCOSE_DAYS,
           missed=_short_of("days_in_range", "glucose_days")),
    Metric("insulin_infusion", "stay_months", "ratio", "insulin_days", "days_present", "days present",
           missed=_any("insulin_days"), missed_label="with any"),
    Metric("glucose_measured", "stay_months", "ratio", "glucose_days", "days_present", "days present",
           missed=_short_of("glucose_days", "days_present")),
]

GLYCEMIC = Dashboard(
    id="glycemic",
    title="ICU Glycemic Control",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(STAY_MONTHS,),
    metrics=METRICS,
    domains=["Hypoglycemia", "Hyperglycemia", "Insulin and monitoring"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    # Patient-days cluster within patients, so no measure gets control limits
    independent_denominators=frozenset(),
)
