"""Aggregate episode or proning rows into a measure value, overall or by group.

Same arithmetic as upstream web/src/metrics.js aggregate() and pChart():
ratios are pooled sums (100 × Σnum / Σden over rows with a denominator),
means carry SD and IQR, medians carry IQR, and quantiles interpolate linearly.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .registry import Metric


def _finite(value):
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) or math.isinf(value) else value


def aggregate(metric: Metric, rows: pd.DataFrame) -> dict:
    """One aggregate for all `rows`."""
    if metric.kind == "ratio":
        if metric.denominator:
            denominators = rows[metric.denominator].fillna(0) if len(rows) else pd.Series(dtype=float)
        else:
            denominators = pd.Series(1.0, index=rows.index)
        counted = rows[denominators != 0]
        denominator = float(denominators[denominators != 0].sum())
        numerator = float(counted[metric.numerator].fillna(0).sum()) if len(counted) else 0.0
        result = {"num": _int_if_whole(numerator), "den": _int_if_whole(denominator), "n": _int_if_whole(denominator),
                  "value": 100 * numerator / denominator if denominator else None}
        if metric.expected and metric.expected in counted.columns:
            expected = counted[metric.expected].dropna()
            if len(expected):
                expected_count = float(expected.sum())
                result["expected"] = 100 * expected_count / len(expected)
                result["oe"] = numerator / expected_count if expected_count else None
                result["expectedCount"] = expected_count
        return result

    values = rows[metric.numerator].dropna().astype(float).to_numpy() if len(rows) else np.array([])
    quartiles = np.quantile(values, [0.25, 0.5, 0.75]) if len(values) else [None, None, None]
    if metric.kind == "mean":
        return {"value": _finite(values.mean()) if len(values) else None, "n": int(len(values)),
                "sd": _finite(values.std(ddof=1)) if len(values) > 1 else None,
                "q1": _finite(quartiles[0]), "q3": _finite(quartiles[2])}
    return {"value": _finite(quartiles[1]), "n": int(len(values)), "q1": _finite(quartiles[0]),
            "q3": _finite(quartiles[2])}


def _int_if_whole(value: float):
    return int(value) if float(value).is_integer() else value


def by_group(metric: Metric, rows: pd.DataFrame, column: str, keys=None) -> list[dict]:
    """Aggregate per value of `column` (missing values grouped as "Unknown"), largest n first.

    With `keys`, return exactly those groups in that order, empty ones included.
    """
    labels = rows[column].astype(object).where(rows[column].notna(), "Unknown") if len(rows) else pd.Series(dtype=object)
    groups = {key: part for key, part in rows.groupby(labels, sort=False)} if len(rows) else {}
    if keys is not None:
        empty = rows.iloc[0:0]
        return [{"key": key, **aggregate(metric, groups.get(key, empty))} for key in keys]
    out = [{"key": str(key), **aggregate(metric, part)} for key, part in groups.items()]
    return sorted(out, key=lambda g: -(g.get("n") or 0))


def monthly(metric: Metric, rows: pd.DataFrame, months: list[str]) -> list[dict]:
    return [{"month": g.pop("key"), **g} for g in by_group(metric, rows, "month", keys=months)]


def p_chart(points: list[dict]) -> dict | None:
    """3-sigma limits around the pooled proportion, per month."""
    numerator = sum(p.get("num") or 0 for p in points)
    denominator = sum(p.get("den") or 0 for p in points)
    if not denominator:
        return None
    pbar = numerator / denominator
    limits = []
    for p in points:
        if not p.get("den"):
            limits.append(None)
            continue
        se = math.sqrt(pbar * (1 - pbar) / p["den"])
        limits.append({"lo": max(0.0, 100 * (pbar - 3 * se)), "hi": min(100.0, 100 * (pbar + 3 * se))})
    return {"center": 100 * pbar, "limits": limits}
