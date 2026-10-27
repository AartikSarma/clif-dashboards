"""Dashboard modules the platform can build and serve, by id."""
from __future__ import annotations

from .fluids import FLUIDS
from .microbiology import MICROBIOLOGY
from .respiratory import RESPIRATORY
from .stewardship import STEWARDSHIP

DASHBOARDS = {d.id: d for d in (RESPIRATORY, STEWARDSHIP, MICROBIOLOGY, FLUIDS)}
DEFAULT_DASHBOARD = RESPIRATORY
