"""Dashboard modules the platform can build and serve, by id."""
from __future__ import annotations

from .flow import FLOW
from .fluids import FLUIDS
from .glycemic import GLYCEMIC
from .kidney import KIDNEY
from .liberation import LIBERATION
from .microbiology import MICROBIOLOGY
from .respiratory import RESPIRATORY
from .stewardship import STEWARDSHIP

DASHBOARDS = {d.id: d for d in (RESPIRATORY, STEWARDSHIP, MICROBIOLOGY, FLUIDS, FLOW, GLYCEMIC, LIBERATION, KIDNEY)}
DEFAULT_DASHBOARD = RESPIRATORY
