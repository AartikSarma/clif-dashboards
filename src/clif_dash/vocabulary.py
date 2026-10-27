"""CLIF 3.0 category values read as their CLIF 2.1 equivalents.

CLIF 3.0 kept the 2.1 columns of every table the dashboards read but rewrote
their category values: lower snake case everywhere (IMV -> imv, Female ->
female, DNR/DNI -> dnr_or_dni) and a few renames (Assist Control-Volume Control
-> acvc, Pressure Support/CPAP -> ps_or_cpap, Expired -> expired). The
respiratory measures (upstream's code and clifpy's waterfall) compare against
2.1 values, so a 3.0 site's tables are translated back when they are loaded
into pandas. 2.1 values pass through unchanged, so a 2.1 site reads exactly as
before.

The translation inverts clifpy's own 2.1 -> 3.0 crosswalk
(clifpy.utils.crosswalk: its normalizer plus the renames in
schemas/crosswalks/clif_2.1_to_3.0.yaml) over every 2.1 permissible value. 3.0
values with no 2.1 equivalent are left as they are, except mode t_piece, which
clifpy's 2.1 waterfall itself records as Blow by.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

# 3.0 values with no 2.1 permissible value that the 2.1 code still has a name for
EXTRA = {("respiratory_support", "mode_category"): {"t_piece": "Blow by"}}


@lru_cache(maxsize=None)
def clif30_to_clif21(table: str) -> dict[str, dict[str, str]]:
    """{column: {3.0 value: 2.1 value}} for a table's category and group columns (empty if unknown)."""
    try:
        import clifpy
        from clifpy.utils.crosswalk import load_crosswalk, normalize_category_value
    except ImportError:
        return {}
    schema_path = Path(clifpy.__file__).parent / "schemas" / "2.1" / f"{table}_schema.yaml"
    if not schema_path.exists():
        return {}
    renames = load_crosswalk().get("renames", {}).get(table, {})
    translations: dict[str, dict[str, str]] = {}
    for column in yaml.safe_load(schema_path.read_text()).get("columns", []):
        values = column.get("permissible_values") or []
        mapping = {}
        for value in values:
            if not isinstance(value, str):
                continue
            new = renames.get(column["name"], {}).get(value) or normalize_category_value(value)
            # A 3.0 value that is also a 2.1 value keeps its 2.1 meaning (clostridioides_difficile)
            if new != value and new not in values:
                mapping.setdefault(new, value)
        mapping.update(EXTRA.get((table, column["name"]), {}))
        if mapping:
            translations[column["name"]] = mapping
    return translations


def to_clif21(table: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Rewrite any CLIF 3.0 category values in `frame` (a CLIF `table`) as their 2.1 values, in place."""
    for column, mapping in clif30_to_clif21(table).items():
        if column not in frame.columns or not len(frame):
            continue
        values = frame[column]
        if not (pd.api.types.is_object_dtype(values) or pd.api.types.is_string_dtype(values)
                or isinstance(values.dtype, pd.CategoricalDtype)):
            continue
        values = values.astype(object)
        hit = values.isin(mapping.keys())
        if hit.any():
            frame[column] = values.where(~hit, values.map(mapping))
    return frame


def category_name_21(table: str, column: str, value: str) -> str:
    """One category value as its 2.1 name (`glucose` -> `glucose_fingerstick`); unchanged if already 2.1."""
    return clif30_to_clif21(table).get(column, {}).get(value, value)
