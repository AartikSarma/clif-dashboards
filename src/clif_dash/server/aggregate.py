"""Aggregate episode or proning rows into a measure value, overall or by group.

Same arithmetic as upstream web/src/metrics.js aggregate() and pChart():
ratios are pooled sums (100 × Σnum / Σden over rows with a denominator),
means carry SD and IQR, medians carry IQR, and quantiles interpolate linearly.

Rates are not upstream's: events per `scale` units of exposure (deaths per
1,000 ICU days, antibiotic days per 1,000 patient-days), pooled the same way
as ratios, with u-chart limits instead of p-chart limits.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..cube import N_ROWS, histogram_values, is_cube
from ..dashboard import Metric


def _finite(value):
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) or math.isinf(value) else value


def aggregate(metric: Metric, rows: pd.DataFrame) -> dict:
    """One aggregate for all `rows`: patient rows, or a cube of them (src/clif_dash/cube.py)."""
    cube = is_cube(rows)
    if metric.kind == "rate":
        exposure = rows[metric.denominator].fillna(0) if len(rows) else pd.Series(dtype=float)
        counted = rows[exposure > 0]
        exposure_total = float(exposure[exposure > 0].sum())
        events = float(counted[metric.numerator].fillna(0).sum()) if len(counted) else 0.0
        # n counts the rows that contribute exposure (stays, patients), not units of exposure
        n = int(counted[f"{metric.denominator}__nz"].sum()) if cube else int(len(counted))
        return {"num": _int_if_whole(events), "den": _int_if_whole(exposure_total), "n": n,
                "value": metric.scale * events / exposure_total if exposure_total else None}
    if metric.kind == "ratio":
        if metric.denominator:
            denominators = rows[metric.denominator].fillna(0) if len(rows) else pd.Series(dtype=float)
        else:
            # One per row; a cube cell stands for n_rows rows
            denominators = rows[N_ROWS].astype(float) if cube else pd.Series(1.0, index=rows.index)
        counted = rows[denominators != 0]
        denominator = float(denominators[denominators != 0].sum())
        numerator = float(counted[metric.numerator].fillna(0).sum()) if len(counted) else 0.0
        result = {"num": _int_if_whole(numerator), "den": _int_if_whole(denominator), "n": _int_if_whole(denominator),
                  "value": 100 * numerator / denominator if denominator else None}
        if metric.expected and cube and f"{metric.id}__expected_n" in counted.columns:
            expected_n = float(counted[f"{metric.id}__expected_n"].sum())
            if expected_n:
                expected_count = float(counted[f"{metric.id}__expected_sum"].sum())
                result["expected"] = 100 * expected_count / expected_n
                result["oe"] = numerator / expected_count if expected_count else None
                result["expectedCount"] = expected_count
        elif metric.expected and metric.expected in counted.columns:
            expected = counted[metric.expected].dropna()
            if len(expected):
                expected_count = float(expected.sum())
                result["expected"] = 100 * expected_count / len(expected)
                result["oe"] = numerator / expected_count if expected_count else None
                result["expectedCount"] = expected_count
        return result

    if cube:
        # Means from exported sums; quartiles from the histogram, exact to within a bin (about 1%)
        values = histogram_values(rows, metric.numerator) if len(rows) else np.array([])
        n = int(rows[f"{metric.numerator}__n"].sum()) if len(rows) else 0
        total = float(rows[f"{metric.numerator}__sum"].sum()) if len(rows) else 0.0
        squares = float(rows[f"{metric.numerator}__sumsq"].sum()) if len(rows) else 0.0
        mean = total / n if n else None
        sd = math.sqrt(max(0.0, (squares - total * total / n) / (n - 1))) if n > 1 else None
    else:
        values = rows[metric.numerator].dropna().astype(float).to_numpy() if len(rows) else np.array([])
        n = int(len(values))
        mean = values.mean() if n else None
        sd = values.std(ddof=1) if n > 1 else None
    quartiles = np.quantile(values, [0.25, 0.5, 0.75]) if len(values) else [None, None, None]
    if metric.kind == "mean":
        return {"value": _finite(mean), "n": n, "sd": _finite(sd), "q1": _finite(quartiles[0]),
                "q3": _finite(quartiles[2])}
    return {"value": _finite(quartiles[1]), "n": n, "q1": _finite(quartiles[0]), "q3": _finite(quartiles[2])}


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def event_times(rows: pd.DataFrame, time_column: str, tz: str, where: str | None = None) -> pd.Series:
    """The local times of a table's events: rows with a time, and with `where` true if given, up to the
    last month those rows are attributed to (a transfer out in January for a November admission at the
    end of the data is left out). An exported heatmap table applies the same rule in the server."""
    if where is not None and len(rows):
        rows = rows[rows[where].fillna(0).astype(bool)]
    rows = rows[rows[time_column].notna()] if len(rows) else rows
    times = rows[time_column].dt.tz_convert(tz) if len(rows) else pd.Series(dtype="datetime64[ns, UTC]")
    if len(times) and "month" in rows.columns and rows["month"].notna().any():
        times = times[times.dt.strftime("%Y-%m") <= rows["month"].max()]
    return times


def days_by_weekday(first_month: str, last_month: str) -> list[int]:
    """How many Mondays, Tuesdays, ... Sundays fall in the whole months from first_month to last_month."""
    days = pd.date_range(pd.Period(first_month, "M").start_time, pd.Period(last_month, "M").end_time.normalize())
    return pd.Series(days.dayofweek).value_counts().reindex(range(7), fill_value=0).astype(int).tolist()


def event_counts(times: pd.Series) -> pd.DataFrame:
    """Local event times as counts by month, day of week (0 = Monday) and hour: a cube's event table."""
    return pd.DataFrame({"event_month": times.dt.strftime("%Y-%m"), "weekday": times.dt.dayofweek,
                         "hour": times.dt.hour}).groupby(["event_month", "weekday", "hour"]).size() \
        .rename("count").reset_index()


def hour_by_weekday(counts: pd.DataFrame) -> dict:
    """Events by day of week (7 rows, Monday first) and local hour (24 columns), over the months they span.

    `counts` has event_month, weekday, hour and count (event_counts, or an exported
    heatmap table). `counts` are totals; `days` is how many of each weekday the period
    holds, so the interface can show the average per day.
    """
    counts = counts[counts["count"] > 0]
    if not len(counts):
        return {"weekdays": WEEKDAYS, "hours": list(range(24)), "counts": [[] for _ in range(7)], "days": [],
                "period": [], "n": 0}
    first, last = counts["event_month"].min(), counts["event_month"].max()
    table = counts.groupby(["weekday", "hour"])["count"].sum().unstack("hour") \
        .reindex(index=range(7), columns=range(24)).fillna(0)
    return {"weekdays": WEEKDAYS, "hours": list(range(24)), "counts": table.to_numpy().astype(int).tolist(),
            "days": days_by_weekday(first, last), "period": [first, last], "n": int(table.to_numpy().sum())}


def binned_by_weekday(rows: pd.DataFrame, weekday_column: str, hour_column: str, numerator: str, denominator: str,
                      scale: float = 1.0) -> dict:
    """Rows binned by day of week (0 = Monday) and hour: scale × Σ numerator ÷ Σ denominator per cell."""
    if not len(rows) or not rows[denominator].fillna(0).gt(0).any():
        return {"weekdays": WEEKDAYS, "hours": list(range(24)), "values": [[] for _ in range(7)],
                "weights": [[] for _ in range(7)], "period": [], "n": 0}
    rows = rows[rows[denominator].fillna(0) > 0]
    sums = rows.groupby([weekday_column, hour_column])[[numerator, denominator]].sum()
    shape = lambda column: sums[column].unstack(hour_column).reindex(index=range(7), columns=range(24)).fillna(0.0)
    top, bottom = shape(numerator), shape(denominator)
    values = (scale * top / bottom.where(bottom > 0)).round(4)
    return {"weekdays": WEEKDAYS, "hours": list(range(24)),
            "values": [[None if pd.isna(v) else float(v) for v in row] for row in values.to_numpy()],
            "weights": bottom.round(2).to_numpy().tolist(), "n": float(round(bottom.to_numpy().sum(), 2)),
            "period": [rows["month"].min(), rows["month"].max()],
            "weekday_values": [None if not b else round(scale * t / b, 4)
                               for t, b in zip(top.sum(axis=1), bottom.sum(axis=1))]}


def hour_by_month(counts: pd.DataFrame) -> dict:
    """Events by local hour of day (24 rows) and month (columns, every month from the first to the last),
    from event counts (event_counts, or an exported heatmap table)."""
    counts = counts[counts["count"] > 0]
    if not len(counts):
        return {"months": [], "hours": list(range(24)), "counts": [[] for _ in range(24)], "totals": [], "n": 0}
    months = list(pd.period_range(counts["event_month"].min(), counts["event_month"].max(), freq="M")
                  .strftime("%Y-%m"))
    table = counts.groupby(["hour", "event_month"])["count"].sum().unstack("event_month") \
        .reindex(index=range(24), columns=months).fillna(0)
    return {"months": months, "hours": list(range(24)), "counts": table.to_numpy().astype(int).tolist(),
            "totals": table.sum(axis=0).astype(int).tolist(), "n": int(table.to_numpy().sum())}


def binned_by_month(rows: pd.DataFrame, hour_column: str, numerator: str, denominator: str,
                    scale: float = 1.0) -> dict:
    """Rows binned by hour of day and month: scale × Σ numerator ÷ Σ denominator per cell, and Σ denominator.

    A cell with no denominator has no value.
    """
    if not len(rows) or not rows[denominator].fillna(0).gt(0).any():
        return {"months": [], "hours": list(range(24)), "values": [[] for _ in range(24)],
                "weights": [[] for _ in range(24)], "n": 0}
    rows = rows[rows[denominator].fillna(0) > 0]
    months = list(pd.period_range(rows["month"].min(), rows["month"].max(), freq="M").strftime("%Y-%m"))
    sums = rows.groupby([hour_column, "month"])[[numerator, denominator]].sum()
    shape = lambda column: (sums[column].unstack("month").reindex(index=range(24), columns=months).fillna(0.0))
    top, bottom = shape(numerator), shape(denominator)
    values = (scale * top / bottom.where(bottom > 0)).round(4)
    return {"months": months, "hours": list(range(24)),
            "values": [[None if pd.isna(v) else float(v) for v in row] for row in values.to_numpy()],
            "weights": bottom.round(2).to_numpy().tolist(), "n": float(round(bottom.to_numpy().sum(), 2)),
            "hour_values": [None if not b else round(scale * t / b, 4)
                            for t, b in zip(top.sum(axis=1), bottom.sum(axis=1))]}


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
    return {"kind": "p", "center": 100 * pbar, "limits": limits}


def u_chart(points: list[dict], scale: float) -> dict | None:
    """3-sigma limits around the pooled rate, per month: ū ± 3·√(ū / exposure), times `scale`."""
    events = sum(p.get("num") or 0 for p in points)
    exposure = sum(p.get("den") or 0 for p in points)
    if not exposure:
        return None
    ubar = events / exposure
    limits = []
    for p in points:
        if not p.get("den"):
            limits.append(None)
            continue
        se = math.sqrt(ubar / p["den"])
        limits.append({"lo": max(0.0, scale * (ubar - 3 * se)), "hi": scale * (ubar + 3 * se)})
    return {"kind": "u", "center": scale * ubar, "limits": limits}


def control_limits(metric: Metric, points: list[dict]) -> dict | None:
    """p-chart limits for a ratio, u-chart limits for a rate; none for means and medians."""
    if metric.kind == "ratio":
        return p_chart(points)
    if metric.kind == "rate":
        return u_chart(points, metric.scale)
    return None
