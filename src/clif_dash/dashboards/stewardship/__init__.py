"""Antimicrobial stewardship: NHSN-style days of therapy per 1,000 days present, by ICU."""
from __future__ import annotations

from ...dashboard import Dashboard, Metric, RowTable
from . import build

STAY_MONTHS = RowTable("stay_months", "row_id", "ICU stay-month", "ICU stay-months")


def _used(column):
    """The list's selection for a use measure: stay-months in which the patient received any of its agents."""
    return lambda rows, metric: rows[column].fillna(0) > 0


USED = "with any"


METRICS = [
    Metric("all_antibacterial", "stay_months", "rate", "dot_all_antibacterial", "days_present", "days present",
           scale=1000, missed=_used("dot_all_antibacterial"), missed_label=USED),
    Metric("antibacterial_days_share", "stay_months", "ratio", "days_any", "days_present", "days present",
           missed=_used("days_any"), missed_label=USED),
    *[Metric(group, "stay_months", "rate", f"dot_{group}", "days_present", "days present", scale=1000,
             missed=_used(f"dot_{group}"), missed_label=USED)
      for group in build.GROUPS if group != "all_antibacterial"],
    Metric("redundant_anaerobic", "stay_months", "ratio", "days_redundant_anaerobic", "days_anaerobic",
           "days on anti-anaerobic agents", missed=_used("days_redundant_anaerobic"), missed_label=USED),
]

STEWARDSHIP = Dashboard(
    id="stewardship",
    title="ICU Antimicrobial Stewardship",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(STAY_MONTHS,),
    metrics=METRICS,
    domains=["Overall use", "Agent groups (NHSN)", "Anaerobic coverage"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    # Days of therapy cluster within patients, so no measure here gets control limits
    independent_denominators=frozenset(),
)
