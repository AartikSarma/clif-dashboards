"""Aggregate tables: a dashboard's rows summed over every combination of the filters.

An export (src/clif_dash/export.py) turns each row table into a cube: one row per
combination of month, hospital, unit, sex, race and ethnicity, age band and the
dashboard's own filters, holding `n_rows` and the sum of every column a measure
adds up. Ratios, rates, observed/expected and control limits come out of the sums
exactly as from the patient rows. Means use exported sums and sums of squares;
medians and IQRs use a histogram of each value in fine bins, a little over 1% wide,
so they are exact to within a bin. Nothing in a cube identifies a patient, but a
cell can still describe one when the filters narrow it far enough.

The server reads cubes and patient rows with the same code: a table with an
`n_rows` column is a cube (`is_cube`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

N_ROWS = "n_rows"
# Columns that place a row in a cell, when the table has them; the dashboard's own filters are added
CELL_KEYS = ("month", "hospital", "unit", "sex", "race_eth", "age_band")
BINS_PER_DECADE = 200         # 10 ** (1 / 200) - 1: bins about 1.2% wide
ZERO_BIN = 0
NEGATIVE_OFFSET = 10_000_000  # negative values: their own range of bin numbers


def is_cube(rows: pd.DataFrame) -> bool:
    return N_ROWS in rows.columns


def row_count(rows: pd.DataFrame) -> int:
    """Patient rows in a table: its length, or a cube's summed n_rows."""
    return int(rows[N_ROWS].sum()) if is_cube(rows) else int(len(rows))


def value_bin(values: pd.Series) -> pd.Series:
    """A bin number for each value: 0 for zero, 1 + log-scale steps above it, and the same mirrored below."""
    values = values.astype(float)
    magnitude = np.log10(values.abs().where(values != 0)) * BINS_PER_DECADE
    steps = magnitude.round().fillna(0).astype("int64") + NEGATIVE_OFFSET // 2   # keep log steps positive
    bins = pd.Series(ZERO_BIN, index=values.index, dtype="int64")
    bins[values > 0] = steps[values > 0]
    bins[values < 0] = NEGATIVE_OFFSET + steps[values < 0]
    return bins


def bin_value(bins: np.ndarray) -> np.ndarray:
    """The centre value of each bin number (inverse of value_bin)."""
    bins = np.asarray(bins, dtype="int64")
    negative = bins >= NEGATIVE_OFFSET
    steps = np.where(negative, bins - NEGATIVE_OFFSET, bins) - NEGATIVE_OFFSET // 2
    values = np.power(10.0, steps / BINS_PER_DECADE)
    values = np.where(negative, -values, values)
    return np.where(bins == ZERO_BIN, 0.0, values)


def histogram_values(rows: pd.DataFrame, column: str) -> np.ndarray:
    """The values a cube's histogram of `column` stands for, each bin's centre repeated by its count."""
    bins = rows[f"{column}__bins"].dropna()
    counts = rows[f"{column}__counts"].dropna()
    if not len(bins):
        return np.array([])
    flat_bins = np.concatenate([np.asarray(b, dtype="int64") for b in bins])
    flat_counts = np.concatenate([np.asarray(c, dtype="int64") for c in counts])
    return np.repeat(bin_value(flat_bins), flat_counts)
