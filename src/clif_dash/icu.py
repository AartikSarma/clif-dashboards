"""ICU time shared by dashboards that count events per ICU day.

Days present, placing a timed event (a dose, a culture) in the ICU stay that
contains it, and the whole months a site's records cover. The stewardship and
microbiology dashboards use these, so both count ICU days the same way.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import SiteConfig
from .dashboard import SiteBuild
from .upstream import prep

DAY_KEY = ["encounter_block", "hospital", "unit", "day"]
ONE_MICROSECOND = pd.Timedelta(microseconds=1)


def lower(series: pd.Series) -> pd.Series:
    return series.astype("string").str.lower()


def icu_intervals(site: SiteBuild, location_category: str = "icu") -> tuple[pd.DataFrame, int]:
    """Adult encounters' ICU ADT rows with an end time; also how many rows had none at all.

    A row with no out time ends at the encounter's discharge; a row with neither is left out.
    Units are labelled from location_type, as on every dashboard.
    """
    adults = site.encounters.loc[site.encounters["adult"], ["encounter_block", "discharge_dttm"]]
    adt = site.adt[lower(site.adt["location_category"]).eq(location_category.lower()).fillna(False)]
    adt = adt.merge(adults, on="encounter_block")
    adt = adt.assign(out_dttm=adt["out_dttm"].fillna(adt["discharge_dttm"]))
    unended = int(adt["out_dttm"].isna().sum())
    adt = adt[adt["out_dttm"].notna() & (adt["out_dttm"] > adt["in_dttm"])]
    adt = adt.reset_index(drop=True)
    intervals = pd.DataFrame({
        "encounter_block": adt["encounter_block"],
        "hospital": adt["hospital_id"].astype(str),
        "unit": adt["location_type"].map(prep.unit_label),
        "in_dttm": adt["in_dttm"],
        "out_dttm": adt["out_dttm"],
    })
    return intervals.sort_values(["encounter_block", "in_dttm"], kind="stable").reset_index(drop=True), unended


def days_present(intervals: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Every local calendar day each ICU row touches, once per encounter, hospital and unit.

    A stay that ends exactly at midnight does not count the next day.
    """
    if intervals.empty:
        return pd.DataFrame(columns=DAY_KEY)
    first = intervals["in_dttm"].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    last = (intervals["out_dttm"] - ONE_MICROSECOND).dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    n_days = ((last - first).dt.days + 1).to_numpy()
    expanded = intervals.loc[intervals.index.repeat(n_days), ["encounter_block", "hospital", "unit"]]
    offsets = np.concatenate([np.arange(n) for n in n_days])
    expanded["day"] = first.repeat(n_days).to_numpy() + pd.to_timedelta(offsets, unit="D")
    return expanded.drop_duplicates(DAY_KEY).reset_index(drop=True)


def place_in_icu(events: pd.DataFrame, intervals: pd.DataFrame, time_column: str, tz: str) -> pd.DataFrame:
    """Events (with encounter_block and a UTC time) placed in the ICU row that contains them.

    in <= time < out; an event in two overlapping rows keeps the earlier row; events outside
    every ICU row are dropped. Adds hospital, unit and the local `day`.
    """
    events = events.assign(_event=np.arange(len(events)))
    placed = events.merge(intervals, on="encounter_block")
    placed = placed[(placed["in_dttm"] <= placed[time_column]) & (placed[time_column] < placed["out_dttm"])]
    placed = placed.sort_values(["_event", "in_dttm"], kind="stable").drop_duplicates("_event")
    placed = placed.drop(columns=["_event", "in_dttm", "out_dttm"])
    placed["day"] = placed[time_column].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    return placed.reset_index(drop=True)


def in_window(days: pd.Series, cfg: SiteConfig) -> pd.Series:
    keep = pd.Series(True, index=days.index)
    if cfg.start_date:
        keep &= days >= pd.Timestamp(cfg.start_date)
    if cfg.end_date:
        keep &= days <= pd.Timestamp(cfg.end_date)
    return keep


def utc_text(value) -> str | None:
    """A DuckDB timestamp as UTC text, so it pickles to workers and saves to summary.json."""
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    return str(stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC"))


def whole_months(first_record: str | None, last_record: str | None, first_admission: str | None,
                 last_admission: str | None, tz: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """The first and last local days of the whole months inside both a table's records and
    the span of hospital admissions.

    Before a table's records begin, ICU days cannot show its events, so rates read falsely
    low. After the last admission, the ICU holds only patients admitted earlier, who stay
    longest, so rates are skewed. The edge months are only partly recorded. None when the
    table has no records.
    """
    if not first_record:
        return None
    local = lambda text: pd.Timestamp(text).tz_convert(tz).tz_localize(None).normalize()
    first = max(local(first_record), local(first_admission)) if first_admission else local(first_record)
    last = min(local(last_record), local(last_admission)) if last_admission else local(last_record)
    start = first if first.day == 1 else first + pd.offsets.MonthBegin(1)
    end = last if last.is_month_end else last.replace(day=1) - pd.Timedelta(days=1)
    return start, end
