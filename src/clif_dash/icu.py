"""ICU time shared by dashboards that count events per ICU day.

Days present, placing a timed event (a dose, a culture) in the ICU stay that
contains it, and the whole months a site's records cover. Every dashboard that
counts per ICU day uses these, so all of them count ICU days the same way.

Large tables (labs, assessments, infusions, input) are placed in DuckDB inside
`icu_session`, so that only per-day summaries reach pandas.
"""
from __future__ import annotations

from contextlib import contextmanager

import numpy as np
import pandas as pd

from . import stage
from .config import SiteConfig
from .dashboard import SiteBuild
from .upstream import clif_io, prep
from .vocabulary import category_name_21

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


def icu_time_per_day(intervals: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Hours spent in each unit on each local calendar day, per encounter (DAY_KEY + `hours`).

    Each ICU row is clipped to the local days it touches, so a stay from 22:00 to 02:00
    gives two hours to each day, and a daylight-saving day can hold 23 or 25 hours.
    Overlapping rows in one unit are capped at the day's length.
    """
    if intervals.empty:
        return pd.DataFrame(columns=DAY_KEY + ["hours"])
    first = intervals["in_dttm"].dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    last = (intervals["out_dttm"] - ONE_MICROSECOND).dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)
    n_days = ((last - first).dt.days + 1).to_numpy()
    expanded = intervals.loc[intervals.index.repeat(n_days)].reset_index(drop=True)
    offsets = np.concatenate([np.arange(n) for n in n_days])
    expanded["day"] = first.repeat(n_days).to_numpy() + pd.to_timedelta(offsets, unit="D")
    local_midnight = lambda days: days.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous=False).dt.tz_convert("UTC")
    day_start = local_midnight(expanded["day"])
    day_end = local_midnight(expanded["day"] + pd.Timedelta(days=1))
    start = expanded["in_dttm"].where(expanded["in_dttm"] > day_start, day_start)
    end = expanded["out_dttm"].where(expanded["out_dttm"] < day_end, day_end)
    expanded["hours"] = (end - start).dt.total_seconds() / 3600
    expanded["day_hours"] = (day_end - day_start).dt.total_seconds() / 3600
    per_day = expanded.groupby(DAY_KEY).agg(hours=("hours", "sum"), day_hours=("day_hours", "first")).reset_index()
    per_day["hours"] = per_day[["hours", "day_hours"]].min(axis=1)
    return per_day[DAY_KEY + ["hours"]]


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


# --------------------------------------------------------------------------- #
# Placing large tables in DuckDB
# --------------------------------------------------------------------------- #
@contextmanager
def icu_session(con, cfg: SiteConfig, intervals: pd.DataFrame, mapping: pd.DataFrame):
    """Register the ICU rows and the encounter map, and group days in the site's zone.

    Inside it, CAST(<timestamptz> AS DATE) is the local day, and the SQL from
    `placed_sql` and `infusion_days_sql` can run.
    """
    con.register("icu_rows", intervals)
    con.register("encounter_map", mapping[["hospitalization_id", "encounter_block"]])
    con.execute(f"SET TimeZone = '{cfg.site_timezone}'")
    try:
        yield con
    finally:
        con.execute("SET TimeZone = 'UTC'")
        con.unregister("icu_rows")
        con.unregister("encounter_map")


def utc_timestamp(con, source: str, column: str) -> str:
    """SQL for a CLIF datetime column as a UTC timestamp with time zone, however the site stored it.

    CLIF datetimes are UTC: a parquet timestamp without a zone is read as UTC, not as the
    session's zone, and text is parsed with its offset.
    """
    name = column.split(".")[-1]
    kind = con.execute(f"SELECT column_type FROM (DESCRIBE SELECT {name} FROM {source})").fetchone()[0].upper()
    if "TIME ZONE" in kind:
        return column
    if kind.startswith("TIMESTAMP"):
        return f"timezone('UTC', {column})"
    return f"CAST({column} AS TIMESTAMPTZ)"


def source_sql(cfg: SiteConfig, table: str) -> str:
    """A CLIF table read with each row's position, which identifies it once joined."""
    return f"read_parquet('{cfg.table_path(table)}', file_row_number = true)"


def placed_sql(con, cfg: SiteConfig, table: str, time_column: str, columns: str, where: str = "TRUE") -> str:
    """SQL for the rows of `table` (alias s) placed in the ICU row that contains them.

    in <= time < out; a row in two overlapping ICU rows keeps the earlier one; rows
    outside every ICU row are dropped. Gives encounter_block, hospital, unit, event_dttm,
    the local `day`, and `columns` (SQL over s). Run inside `icu_session`.
    """
    source = source_sql(cfg, table)
    time = utc_timestamp(con, source, f"s.{time_column}")
    return f"""
        SELECT i.encounter_block, i.hospital, i.unit, {time} AS event_dttm, CAST({time} AS DATE) AS day, {columns}
        FROM {source} AS s
        JOIN encounter_map AS m ON CAST(s.hospitalization_id AS VARCHAR) = CAST(m.hospitalization_id AS VARCHAR)
        JOIN icu_rows AS i ON i.encounter_block = m.encounter_block AND i.in_dttm <= {time} AND {time} < i.out_dttm
        WHERE s.{time_column} IS NOT NULL AND ({where})
        QUALIFY row_number() OVER (PARTITION BY s.file_row_number ORDER BY i.in_dttm) = 1
    """


STOPPED_ACTIONS = ("stop", "stopped", "paused", "hold", "held")


def infusion_runs_sql(con, cfg: SiteConfig, agents: dict[str, list[str]], last_hours: float = 6) -> str:
    """SQL for the intervals in which each kind of continuous infusion runs, per encounter.

    `agents` maps a kind (sedative, paralytic, insulin) to med_category values. A charted
    dose > 0 runs until the next charted row for the same drug, or `last_hours` after the
    last one; a stop, pause or hold, or an action group not_administered, is a dose of 0.
    The respiratory dashboard's infusion intervals follow the same rule
    (upstream prep.infusion_intervals). Gives encounter_block, kind, start_dttm, end_dttm.
    """
    source = source_sql(cfg, "medication_admin_continuous")
    time = utc_timestamp(con, source, "s.admin_dttm")
    kinds = " ".join(f"WHEN lower(CAST(s.med_category AS VARCHAR)) IN ({quoted(v)}) THEN '{k}'"
                     for k, v in agents.items())
    every = quoted([a for v in agents.values() for a in v])
    columns = set(stage.source_columns(cfg, "medication_admin_continuous"))
    stops = [f"lower(CAST(s.mar_action_category AS VARCHAR)) IN ({quoted(STOPPED_ACTIONS)})"
             if "mar_action_category" in columns else None,
             "lower(CAST(s.mar_action_group AS VARCHAR)) = 'not_administered'" if "mar_action_group" in columns else None]
    stopped = " OR ".join(c for c in stops if c) or "FALSE"
    return f"""
        SELECT encounter_block, kind, start_dttm, end_dttm FROM (
            SELECT m.encounter_block, CASE {kinds} END AS kind, {time} AS start_dttm,
                   coalesce(lead({time}) OVER (PARTITION BY m.encounter_block, lower(CAST(s.med_category AS VARCHAR))
                                               ORDER BY {time}, s.file_row_number),
                            {time} + INTERVAL {int(last_hours * 60)} MINUTE) AS end_dttm,
                   CASE WHEN coalesce({stopped}, FALSE) THEN 0
                        ELSE coalesce(TRY_CAST(s.med_dose AS DOUBLE), 0) END AS dose
            FROM {source} AS s
            JOIN encounter_map AS m ON CAST(s.hospitalization_id AS VARCHAR) = CAST(m.hospitalization_id AS VARCHAR)
            WHERE s.admin_dttm IS NOT NULL AND lower(CAST(s.med_category AS VARCHAR)) IN ({every})
        ) WHERE dose > 0 AND end_dttm > start_dttm
    """


def interval_days_sql(intervals: str) -> str:
    """SQL for the local ICU days each interval touches, per kind (DAY_KEY + kind).

    `intervals` is SQL giving encounter_block, kind, start_dttm, end_dttm; each is clipped
    to the ICU rows it overlaps. Run inside `icu_session`.
    """
    return f"""
        SELECT DISTINCT encounter_block, hospital, unit, CAST(day AS DATE) AS day, kind FROM (
            SELECT r.encounter_block, i.hospital, i.unit, r.kind,
                   unnest(generate_series(CAST(CAST(greatest(r.start_dttm, i.in_dttm) AS DATE) AS TIMESTAMP),
                                          CAST(CAST(least(r.end_dttm, i.out_dttm) - INTERVAL 1 MICROSECOND AS DATE)
                                               AS TIMESTAMP),
                                          INTERVAL 1 DAY)) AS day
            FROM ({intervals}) AS r
            JOIN icu_rows AS i ON i.encounter_block = r.encounter_block
                 AND r.start_dttm < i.out_dttm AND r.end_dttm > i.in_dttm
        )
    """


def quoted(values) -> str:
    """Lower-cased SQL string literals, for an IN list."""
    return ", ".join("'" + str(v).lower().replace("'", "''") + "'" for v in values) or "NULL"


def outlier_bounds(table: str, value_column: str, category: str) -> tuple[float, float]:
    """clifpy's plausible range for one category's values (the platform's outliers decision)."""
    category_column = {"labs": "lab_category", "vitals": "vital_category",
                       "patient_assessments": "assessment_category"}.get(table, "")
    name = category_name_21(table, category_column, category).lower()
    rules = {str(k).lower(): v for k, v in clif_io.outlier_config().get(table, {}).get(value_column, {}).items()}
    rule = rules.get(name, {})
    return float(rule.get("min", -np.inf)), float(rule.get("max", np.inf))


def frame_days(frame: pd.DataFrame) -> pd.DataFrame:
    """A DuckDB result's `day` column as a pandas timestamp, matching `days_present`."""
    frame["day"] = pd.to_datetime(frame["day"])
    return frame
