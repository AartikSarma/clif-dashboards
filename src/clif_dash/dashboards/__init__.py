"""Dashboard modules the platform can build and serve, by id."""
from __future__ import annotations

from .respiratory import RESPIRATORY

DASHBOARDS = {d.id: d for d in (RESPIRATORY,)}
DEFAULT_DASHBOARD = RESPIRATORY
