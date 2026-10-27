"""Microbiology: diagnostic stewardship, hospital-onset infections and resistance, by ICU."""
from __future__ import annotations

from ...dashboard import Dashboard, Metric, RowTable
from . import build

STAY_MONTHS = RowTable("stay_months", "row_id", "ICU stay-month", "ICU stay-months")
STARTS = RowTable("antibiotic_starts", "row_id", "antibacterial start", "antibacterial starts")
ISOLATES = RowTable("isolates", "row_id", "first isolate", "first isolates")


def _any(column):
    return lambda rows, metric: rows[column].fillna(0) > 0


METRICS = [
    Metric("blood_cultures", "stay_months", "rate", "blood_draws", "days_present", "days present", scale=1000,
           missed=_any("blood_draws"), missed_label="with any"),
    Metric("urine_cultures", "stay_months", "rate", "urine_draws", "days_present", "days present",
           scale=1000, missed=_any("urine_draws"), missed_label="with any"),
    Metric("blood_culture_contamination", "stay_months", "ratio", "contaminated_draws", "blood_draws",
           "blood culture draws", missed=_any("contaminated_draws"), missed_label="with any"),
    Metric("cultures_before_antibiotics", "antibiotic_starts", "ratio", "cultured", None, "antibacterial starts",
           missed=lambda rows, metric: rows["cultured"].fillna(0) == 0),
    Metric("hospital_onset_bacteremia", "stay_months", "rate", "hob_events", "days_present", "days present",
           scale=1000, missed=_any("hob_events"), missed_label="with any"),
    Metric("hospital_onset_cdi", "stay_months", "rate", "cdi_events", "days_present", "days present",
           scale=10000, missed=_any("cdi_events"), missed_label="with any"),
    Metric("mrsa", "isolates", "ratio", "mrsa_num", "mrsa_den", "S. aureus isolates",
           missed=_any("mrsa_num"), missed_label="with a resistant isolate"),
    Metric("ceftriaxone_ns_enterobacterales", "isolates", "ratio", "ctx_num", "ctx_den", "Enterobacterales isolates",
           missed=_any("ctx_num"), missed_label="with a resistant isolate"),
    Metric("carbapenem_ns_gram_negatives", "isolates", "ratio", "carb_num", "carb_den", "gram-negative isolates",
           missed=_any("carb_num"), missed_label="with a resistant isolate"),
]

MICROBIOLOGY = Dashboard(
    id="microbiology",
    title="ICU Microbiology",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(STAY_MONTHS, STARTS, ISOLATES),
    metrics=METRICS,
    domains=["Diagnostic stewardship", "Hospital-onset infections", "Resistance"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    # Cultures cluster within patients, so rates per day present get no limits; draws, starts and isolates do
    independent_denominators=frozenset({"blood culture draws", "antibacterial starts", "S. aureus isolates",
                                        "Enterobacterales isolates", "gram-negative isolates"}),
)
