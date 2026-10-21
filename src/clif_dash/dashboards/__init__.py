"""Dashboard modules the platform can build and serve, by id."""
from __future__ import annotations

from .respiratory import RESPIRATORY
from .stewardship import STEWARDSHIP

DASHBOARDS = {d.id: d for d in (RESPIRATORY, STEWARDSHIP)}
DEFAULT_DASHBOARD = RESPIRATORY
