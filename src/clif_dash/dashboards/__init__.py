"""Dashboard modules the platform can build and serve, by id."""
from __future__ import annotations

from .icu_stays import ICU_STAYS
from .respiratory import RESPIRATORY

DASHBOARDS = {d.id: d for d in (RESPIRATORY, ICU_STAYS)}
DEFAULT_DASHBOARD = RESPIRATORY
