"""CLIF 3.0 category values read as their CLIF 2.1 equivalents.

CLIF 3.0 kept the 2.1 columns of every table the dashboards read but rewrote
their category values: lower snake case everywhere (IMV -> imv, Female ->
female, DNR/DNI -> dnr_or_dni) and a few renames (Assist Control-Volume Control
-> acvc, Pressure Support/CPAP -> ps_or_cpap, Expired -> expired). The
respiratory measures (upstream's code and clifpy's waterfall) compare against
2.1 values, so a 3.0 site's tables are translated back when they are loaded
into pandas. 2.1 values pass through unchanged; a 2.1 value spelled with other
case or punctuation (EXPIRED, expired) is read as the 2.1 value, since the
upstream code compares exact strings and would otherwise miss it.

The translation inverts clifpy's own 2.1 -> 3.0 crosswalk
(clifpy.utils.crosswalk: its normalizer plus the renames in
schemas/crosswalks/clif_2.1_to_3.0.yaml) over every 2.1 permissible value. 3.0
values with no 2.1 equivalent are left as they are, except T-piece: device
t_piece reads as IMV (a T-piece trial is a breathing trial through the airway,
not liberation from it), and a T-piece row with no mode, or mode t_piece, reads
as Blow by, which is how clifpy's 2.1 waterfall records a T-piece.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

# 3.0 values with no 2.1 permissible value that the 2.1 code still has a name for
EXTRA = {("respiratory_support", "device_category"): {"t_piece": "IMV"},
         ("respiratory_support", "mode_category"): {"t_piece": "Blow by"}}


def normalized(value):
    """clifpy's 3.0 spelling of a value (lower snake case); non-strings unchanged."""
    from clifpy.utils.crosswalk import normalize_category_value
    return normalize_category_value(value)


@lru_cache(maxsize=None)
def clif21_names(table: str) -> dict[str, tuple[frozenset, dict[str, str]]]:
    """Per category or group column: its 2.1 values, and {3.0 or variant spelling, normalized: 2.1 value}.

    Empty when clifpy or the table's 2.1 schema is unavailable.
    """
    try:
        import clifpy
        from clifpy.utils.crosswalk import load_crosswalk
    except ImportError:
        return {}
    schema_path = Path(clifpy.__file__).parent / "schemas" / "2.1" / f"{table}_schema.yaml"
    if not schema_path.exists():
        return {}
    renames = load_crosswalk().get("renames", {}).get(table, {})
    names: dict[str, tuple[frozenset, dict[str, str]]] = {}
    for column in yaml.safe_load(schema_path.read_text()).get("columns", []):
        values = [v for v in column.get("permissible_values") or [] if isinstance(v, str)]
        if not values:
            continue
        lookup: dict[str, str] = {}
        for value in values:
            lookup.setdefault(normalized(value), value)
        for value in values:
            new = renames.get(column["name"], {}).get(value)
            # A 3.0 value that is also a 2.1 value keeps its 2.1 meaning (clostridioides_difficile)
            if new and new not in values:
                lookup.setdefault(normalized(new), value)
        lookup.update(EXTRA.get((table, column["name"]), {}))
        names[column["name"]] = (frozenset(values), lookup)
    return names


def to_clif21(table: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Rewrite category values in `frame` (a CLIF `table`) as their 2.1 values, in place.

    CLIF 3.0 values and 2.1 values spelled differently (EXPIRED, expired, " Expired")
    become the 2.1 value; values with no 2.1 equivalent are left as they are.
    """
    if table == "respiratory_support" and {"device_category", "mode_category"} <= set(frame.columns):
        t_piece = frame["device_category"].astype(object).map(normalized).eq("t_piece") & frame["mode_category"].isna()
        if t_piece.any():
            frame["mode_category"] = frame["mode_category"].astype(object).where(~t_piece, "Blow by")
    for column, (values_21, lookup) in clif21_names(table).items():
        if column not in frame.columns or not len(frame):
            continue
        values = frame[column]
        if not (pd.api.types.is_object_dtype(values) or pd.api.types.is_string_dtype(values)
                or isinstance(values.dtype, pd.CategoricalDtype)):
            continue
        rewrite = {}
        for value in values.dropna().unique():
            if isinstance(value, str) and value not in values_21:
                target = lookup.get(normalized(value))
                if target is not None and target != value:
                    rewrite[value] = target
        if rewrite:
            values = values.astype(object)
            frame[column] = values.where(~values.isin(rewrite.keys()), values.map(rewrite))
    return frame


def category_name_21(table: str, column: str, value: str) -> str:
    """One category value as its 2.1 name (`glucose` -> `glucose_fingerstick`); unchanged if already 2.1."""
    values_21, lookup = clif21_names(table).get(column, (frozenset(), {}))
    return value if value in values_21 else lookup.get(normalized(value), value)
