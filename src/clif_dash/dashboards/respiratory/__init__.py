"""Respiratory failure on invasive ventilation: upstream's ICU quality dashboard."""
from __future__ import annotations

from ...dashboard import Dashboard
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
    metrics=registry.METRICS,
    domains=registry.DOMAINS,
    strata=registry.STRATA,
    completeness=registry.COMPLETENESS,
    independent_denominators=registry.INDEPENDENT_DENOMINATORS,
    resolve=registry.resolve,
    detail=TimelineBuilder,
)
