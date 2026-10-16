"""Metric registry: how each measure aggregates the episode or proning rows.

Ported from upstream web/src/metrics.js so that aggregation runs on the server.
Titles, units, targets and decisions stay in decisions.yaml; this file says only
which columns form each measure and which rows count as having missed it.
"""
from __future__ import annotations

from dataclasses import replace

from ...dashboard import Metric


def _gt0(column):
    return lambda rows: rows[column].fillna(0) > 0


def _short_ratio(num, den):
    return lambda rows: (rows[den] > 0) & (rows[num] < rows[den])


METRICS: list[Metric] = [
    Metric("ltvv", "episodes", "ratio", "ltvv_num", "ltvv_den", "IMV hours", needs=("has_height", "has_vt"),
           missed=lambda r: (r["ltvv_den"] > 0) & (r["ltvv_num"] / r["ltvv_den"].where(r["ltvv_den"] > 0) < 0.8)),
    Metric("vt_first24", "episodes", "mean", "vt24", den_label="episodes", needs=("has_height", "has_vt"),
           missed=lambda r: r["vt24"].notna() & (r["vt24"] > 8)),
    Metric("plateau", "episodes", "ratio", "plat_num", "plat_den", "readings", needs=("has_plateau",),
           missed=_short_ratio("plat_num", "plat_den")),
    Metric("height_doc", "episodes", "ratio", "hdoc_num", "hdoc_den", "episodes", needs=("has_height",),
           missed=lambda r: (r["hdoc_den"] > 0) & (r["hdoc_num"].fillna(0) == 0)),
    Metric("prone_12h", "proning", "ratio", "p12", None, "patients", expected="p12_expected", needs=("has_abg",),
           missed=lambda r: r["p12"].fillna(0) == 0),
    Metric("prone_any", "proning", "ratio", "p24", None, "patients", extra=("p72", "proned"), needs=("has_abg",),
           missed=lambda r: r["p24"].fillna(0) == 0),
    Metric("prone_dose", "proning", "median", "median_session_h", den_label="proned patients",
           missed=lambda r: r["median_session_h"].notna() & (r["median_session_h"] < 16)),
    Metric("sat", "episodes", "ratio", "sat_num", "sat_den", "vent-days",
           parts=(("sat_doc", "Documented"), ("sat_pheno", "EHR phenotype")), needs=("has_rass",),
           missed=_short_ratio("sat_num", "sat_den")),
    Metric("sbt", "episodes", "ratio", "sbt_num", "sbt_den", "vent-days",
           parts=(("sbt_doc", "Documented"), ("sbt_pheno", "EHR phenotype")),
           missed=_short_ratio("sbt_num", "sbt_den")),
    Metric("vfd28", "episodes", "mean", "vfd28", den_label="episodes", missed=lambda r: r["vfd28"] == 0),
    Metric("imv_duration", "episodes", "median", "imv_days", den_label="episodes", missed=lambda r: r["imv_days"] > 7),
    Metric("reintubation", "episodes", "ratio", "reint_num", "ext_den", "extubations", missed=_gt0("reint_num")),
    Metric("light_sedation", "episodes", "ratio", "rass_num", "rass_den", "RASS readings", needs=("has_rass",),
           missed=lambda r: (r["rass_den"] > 0) & (r["rass_num"] / r["rass_den"].where(r["rass_den"] > 0) < 0.5)),
    Metric("deep_sedation", "episodes", "ratio", "deep_num", "deep_den", "episodes", needs=("has_rass",),
           missed=_gt0("deep_num")),
    Metric("paralytic_8h", "episodes", "ratio", "para8_num", "para8_den", "intubated ICU days", missed=_gt0("para8_num")),
    Metric("mortality", "episodes", "ratio", "died", "outcome_den", "encounters", expected="mort_expected",
           missed=_gt0("died")),
    Metric("icu_los", "episodes", "median", "icu_los", den_label="encounters", missed=lambda r: r["icu_los"] > 7),
    Metric("trach", "episodes", "ratio", "trach_any", "outcome_den", "encounters", missed=_gt0("trach_any")),
    Metric("icu_readmit", "episodes", "ratio", "readmit_num", "readmit_den", "ICU transfers out",
           missed=_gt0("readmit_num")),
]
METRICS_BY_ID = {m.id: m for m in METRICS}
DOMAINS = ["Lung protection", "Proning", "Liberation", "Sedation", "Outcomes"]
STRATA = [("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"), ("race_eth", "Race / ethnicity"),
          ("age_band", "Age band"), ("code_status", "Code status at IMV start")]
COMPLETENESS = [
    ("has_height", "Height recorded (for PBW)"), ("has_weight", "Weight recorded"),
    ("has_vt", "Set tidal volume in a volume mode"), ("has_plateau", "Plateau pressure recorded"),
    ("has_rass", "RASS during IMV"), ("has_abg", "Arterial blood gas"),
]
# Denominators whose units are independent enough for p-chart control limits
INDEPENDENT_DENOMINATORS = frozenset({"episodes", "patients", "encounters", "extubations", "ICU transfers out"})


def ltvv_column(threshold) -> str:
    """Same naming as upstream metrics.ltvv_key: 6 -> ltvv_num_6, 6.5 -> ltvv_num_6_5."""
    return "ltvv_num_" + (str(int(threshold)) if float(threshold).is_integer() else str(threshold).replace(".", "_"))


def resolve(route_id: str, decisions: dict, ltvv_thresholds: list) -> tuple[Metric, float | None, float | None]:
    """Route ids carry the LTVV threshold, e.g. 'ltvv@7'. Returns (metric, threshold, default)."""
    metric_id, _, threshold_text = str(route_id).partition("@")
    metric = METRICS_BY_ID.get(metric_id, METRICS[0])
    if metric.id != "ltvv":
        return metric, None, None
    default = next(d["value"] for d in decisions["metrics"]["ltvv"]["decisions"] if d["id"] == "threshold_ml_kg")
    threshold = float(threshold_text) if threshold_text else default
    if threshold not in ltvv_thresholds:
        threshold = default
    numerator = ltvv_column(threshold) if threshold in ltvv_thresholds else "ltvv_num"
    return (replace(metric, numerator=numerator,
                    missed=lambda r: (r["ltvv_den"] > 0) & (r[numerator] / r["ltvv_den"].where(r["ltvv_den"] > 0) < 0.8)),
            threshold, default)
