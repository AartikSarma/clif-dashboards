"""Rates: events per `scale` units of exposure, pooled, with u-chart limits.

Upstream had no rates, so these are checked against hand arithmetic.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from clif_dash.dashboard import Metric
from clif_dash.server import aggregate as agg

RATE = Metric("dot", "days", "rate", "antibiotic_days", "patient_days", "patient-days", scale=1000)
ROWS = pd.DataFrame({
    "month": ["2024-01", "2024-01", "2024-02", "2024-02", "2024-02"],
    "unit": ["MICU", "SICU", "MICU", "MICU", "SICU"],
    "antibiotic_days": [3, 0, 5, None, 2],
    "patient_days": [10, 4, 8.5, 0, 7.5],      # a row with no exposure contributes nothing
})


def test_a_rate_pools_events_over_exposure():
    result = agg.aggregate(RATE, ROWS)
    assert result["num"] == 10 and result["den"] == 30 and result["n"] == 4
    assert result["value"] == pytest.approx(1000 * 10 / 30)


def test_a_rate_with_no_exposure_has_no_value():
    assert agg.aggregate(RATE, ROWS.iloc[0:0])["value"] is None
    assert agg.aggregate(RATE, ROWS[ROWS["patient_days"] == 0])["value"] is None


def test_rates_by_group_and_month():
    by_unit = {g["key"]: g for g in agg.by_group(RATE, ROWS, "unit")}
    assert by_unit["MICU"]["value"] == pytest.approx(1000 * 8 / 18.5)
    assert by_unit["SICU"]["value"] == pytest.approx(1000 * 2 / 11.5)
    series = agg.monthly(RATE, ROWS, ["2024-01", "2024-02", "2024-03"])
    assert [p["den"] for p in series] == [14, 16, 0]
    assert series[2]["value"] is None


def test_u_chart_limits():
    series = agg.monthly(RATE, ROWS, ["2024-01", "2024-02", "2024-03"])
    control = agg.control_limits(RATE, series)
    ubar = 10 / 30
    assert control["kind"] == "u"
    assert control["center"] == pytest.approx(1000 * ubar)
    for point, limit in zip(series[:2], control["limits"][:2]):
        half_width = 3 * math.sqrt(ubar / point["den"])
        assert limit["hi"] == pytest.approx(1000 * (ubar + half_width))
        assert limit["lo"] == pytest.approx(max(0.0, 1000 * (ubar - half_width)))
    assert control["limits"][2] is None


def test_means_and_medians_get_no_control_limits():
    median = Metric("los", "days", "median", "patient_days")
    assert agg.control_limits(median, agg.monthly(median, ROWS, ["2024-01"])) is None
