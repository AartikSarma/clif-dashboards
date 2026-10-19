"""Respiratory failure on invasive ventilation: upstream's ICU quality dashboard."""
from __future__ import annotations

from ...dashboard import Dashboard, RowTable
from . import build, registry
from .timeline import TimelineBuilder

RESPIRATORY = Dashboard(
    id="respiratory",
    title="ICU Respiratory Failure Quality",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(RowTable("episodes", "episode_id", "IMV episode", "IMV episodes"),
                RowTable("proning", "episode_id", "patient in the proning cohort", "patients in the proning cohort")),
    metrics=registry.METRICS,
    domains=registry.DOMAINS,
    strata=registry.STRATA,
    completeness=registry.COMPLETENESS,
    independent_denominators=registry.INDEPENDENT_DENOMINATORS,
    resolve=registry.resolve,
    detail=TimelineBuilder,
)
