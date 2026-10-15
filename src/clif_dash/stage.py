"""Stage CLIF tables into per-chunk parquet partitions with DuckDB.

Upstream read every table whole into pandas, which needs ~15 GB for 10,000
synthetic hospitalizations. Here DuckDB streams each source table once and
writes only the rows of the requested encounters, split by chunk, so a worker
holds one chunk in memory at a time. The same partitions later serve
single-patient timeline queries.

Each staged row keeps its position in the source file (`source_row`) so that a
chunk loaded back into pandas is in the original file order. Upstream logic
takes `first` values after merges and stable sorts, so row order can change
results.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from .config import SiteConfig

# Tables the respiratory quality metrics read, and the key that links each to an encounter
HOSPITALIZATION_KEYED = [
    "respiratory_support", "vitals", "labs", "position", "patient_assessments",
    "medication_admin_continuous", "hospital_diagnosis",
]
PATIENT_KEYED = ["code_status"]
ALL_TABLES = ["patient", "hospitalization", "adt", *HOSPITALIZATION_KEYED, *PATIENT_KEYED]
REQUIRED_TABLES = {"patient", "hospitalization", "adt", "respiratory_support"}
SOURCE_ROW = "source_row"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con


def clif_conventions(frame: pd.DataFrame) -> pd.DataFrame:
    """Same coercions upstream applied on load: UTC datetimes, string IDs."""
    for column in frame.columns:
        if column.endswith("_dttm"):
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    for column in ("hospitalization_id", "patient_id"):
        if column in frame.columns:
            frame[column] = frame[column].astype(str)
    return frame


def read_whole_table(cfg: SiteConfig, table: str) -> pd.DataFrame:
    """Read a small table (patient, hospitalization, adt) whole."""
    path = cfg.table_path(table)
    if not path.exists():
        if table in REQUIRED_TABLES:
            raise FileNotFoundError(f"Required CLIF table not found: {path}")
        return pd.DataFrame()
    return clif_conventions(pd.read_parquet(path))


def stage_table(con, cfg: SiteConfig, table: str, chunk_map: pd.DataFrame, destination: Path) -> int:
    """Write the rows of `table` that belong to `chunk_map` encounters, partitioned by chunk.

    `chunk_map` has columns (hospitalization_id, chunk) or (patient_id, chunk).
    Returns the number of rows staged.
    """
    source = cfg.table_path(table)
    if not source.exists():
        return 0
    key = "patient_id" if "patient_id" in chunk_map.columns else "hospitalization_id"
    con.register("chunk_map", chunk_map[[key, "chunk"]].drop_duplicates())
    destination.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
            SELECT source.* EXCLUDE (file_row_number), source.file_row_number AS {SOURCE_ROW}, chunk_map.chunk
            FROM read_parquet('{source}', file_row_number = true) AS source
            JOIN chunk_map ON CAST(source.{key} AS VARCHAR) = chunk_map.{key}
        ) TO '{destination}' (FORMAT parquet, PARTITION_BY (chunk), OVERWRITE_OR_IGNORE)
    """)
    staged_rows = con.execute(
        f"SELECT count(*) FROM read_parquet('{destination}/*/*.parquet', hive_partitioning = false)"
    ).fetchone()[0] if any(destination.glob("chunk=*")) else 0
    con.unregister("chunk_map")
    return int(staged_rows)


def source_columns(cfg: SiteConfig, table: str) -> list[str]:
    import pyarrow.parquet as pq
    path = cfg.table_path(table)
    return pq.read_schema(path).names if path.exists() else []


def load_staged_chunk(cfg: SiteConfig, stage_name: str, table: str, chunk: int,
                      hospitalization_ids: list[str] | None = None) -> pd.DataFrame:
    """Load one staged chunk in source-file order, shaped like the source table.

    `hospitalization_ids` restricts the rows read (used for single-patient timelines).
    """
    partition = cfg.stage_dir / stage_name / table / f"chunk={chunk}"
    files = sorted(partition.glob("*.parquet"))
    if not files:
        columns = source_columns(cfg, table)
        return clif_conventions(pd.DataFrame(columns=columns)) if columns else pd.DataFrame()
    row_filter = [("hospitalization_id", "in", list(hospitalization_ids))] if hospitalization_ids is not None else None
    if row_filter and table in PATIENT_KEYED:
        row_filter = None
    frame = pd.concat([pd.read_parquet(f, filters=row_filter) for f in files], ignore_index=True)
    frame = frame.sort_values(SOURCE_ROW, kind="stable").drop(columns=[SOURCE_ROW]).reset_index(drop=True)
    if "chunk" in frame.columns:
        frame = frame.drop(columns=["chunk"])
    return clif_conventions(frame)
