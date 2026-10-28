"""ICU flow: readmission, length of stay, night transfers, ED-to-ICU time and where ICU stays end, from ADT."""
from __future__ import annotations

from ...dashboard import Dashboard, Heatmap, Metric, RowTable
from . import build

ENCOUNTERS = RowTable("encounters", "row_id", "ICU encounter", "ICU encounters")


def _flagged(column):
    return lambda rows, metric: rows[column].fillna(0) > 0


def _longest(column):
    """The list's selection for a median: rows above the median of the rows shown."""
    return lambda rows, metric: rows[column] > rows[column].median()


METRICS = [
    Metric("icu_readmit_48h", "encounters", "ratio", "readmitted_48h", "transferred_out", "transfers out of the ICU",
           missed=_flagged("readmitted_48h"), missed_label="readmitted"),
    Metric("night_transfer_out", "encounters", "ratio", "night_transfer_out", "transferred_out",
           "transfers out of the ICU", missed=_flagged("night_transfer_out"), missed_label="transferred at night"),
    Metric("icu_los", "encounters", "median", "icu_days", den_label="ICU encounters",
           missed=_longest("icu_days"), missed_label="above the median"),
    Metric("ed_to_icu_hours", "encounters", "median", "ed_to_icu_hours", den_label="ICU encounters from the ED",
           missed=_longest("ed_to_icu_hours"), missed_label="above the median"),
    Metric("died_in_icu", "encounters", "ratio", "died_in_icu", None, "ICU encounters",
           missed=_flagged("died_in_icu"), missed_label="who died in the ICU"),
    Metric("discharged_from_icu", "encounters", "ratio", "discharged_from_icu", None, "ICU encounters",
           missed=_flagged("discharged_from_icu"), missed_label="discharged from the ICU"),
]

FLOW = Dashboard(
    id="flow",
    title="ICU Flow",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(ENCOUNTERS,),
    metrics=METRICS,
    domains=["Transfers out", "Time", "Where ICU stays end"],
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
            ("age_band", "Age band")],
    filters=(),
    completeness=[],
    # One row per encounter, so encounters and their transfers out are independent enough for p-chart limits
    independent_denominators=frozenset({"ICU encounters", "transfers out of the ICU"}),
    heatmaps=(Heatmap("icu_admission", "ICU admission", "encounters", "icu_admission", "ICU admissions",
                      note="The first ICU ADT row of each encounter."),
              Heatmap("transfer_out", "Transfer out of the ICU", "encounters", "transfer_out_time",
                      "transfers out", where="transferred_out",
                      note="Each encounter's first transfer from an ICU to a non-ICU location, alive.")),
)
