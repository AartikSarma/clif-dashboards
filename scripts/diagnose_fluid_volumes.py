"""Which input volumes are implausible, and do the fluids volume limits catch them? Aggregate-only, safe to share.

    uv run python scripts/diagnose_fluid_volumes.py --config config/<site>.yaml

Prints, for each input_group, the row count and the 50th, 99th and 99.9th percentile
and largest single volume, with how many rows and litres the volume_limits decision
leaves out. Then the months holding the most volume above the limits. No IDs and no
dates finer than a month. Counts below the small-cell threshold (11) print as "<11".

A group whose 99.9th percentile sits far below its limit while its largest rows run to
millions of mL points at entry or unit errors, which the limit removes. A group whose
99th percentile is near its limit points at a site that charts running totals, and the
limit should rise for that group (config/decisions/fluids.yaml).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clif_dash.config import load_config  # noqa: E402
from clif_dash.dashboards.fluids.build import shared_values, utc_timestamp, volume_limit_sql  # noqa: E402

SMALL_CELL = 11


def count(n) -> str:
    n = int(n)
    return f"<{SMALL_CELL}" if 0 < n < SMALL_CELL else f"{n:,}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config).bind("fluids")
    limit = volume_limit_sql(shared_values(cfg.decisions()).get("volume_limits"))
    source = f"read_parquet('{cfg.table_path('input')}')"
    con = duckdb.connect()
    con.execute(f"SET TimeZone = '{cfg.site_timezone}'")

    groups = con.execute(f"""
        SELECT lower(CAST(input_group AS VARCHAR)) AS input_group, count(*) AS rows_positive,
               quantile_cont(input_volume, 0.5) AS p50, quantile_cont(input_volume, 0.99) AS p99,
               quantile_cont(input_volume, 0.999) AS p999, max(input_volume) AS largest,
               max({limit}) AS volume_limit,
               count(*) FILTER (WHERE input_volume > {limit}) AS rows_over,
               coalesce(sum(input_volume) FILTER (WHERE input_volume > {limit}), 0) / 1000 AS litres_over
        FROM {source} WHERE input_volume > 0 GROUP BY 1 ORDER BY 2 DESC
    """).df()
    groups["rows_positive"] = groups["rows_positive"].map(count)
    groups["rows_over"] = groups["rows_over"].map(lambda n: count(n) if n else "0")
    for column in ("p50", "p99", "p999", "largest", "volume_limit", "litres_over"):
        groups[column] = groups[column].map(lambda v: f"{v:,.0f}")
    print("Positive volumes by input_group (mL; litres_over in L)\n")
    print(groups.to_string(index=False))

    recorded = utc_timestamp(con, source, "recorded_dttm")
    months = con.execute(f"""
        SELECT strftime({recorded}, '%Y-%m') AS month,
               count(*) AS rows_over, sum(input_volume) / 1000 AS litres_over
        FROM {source} WHERE input_volume > {limit} AND recorded_dttm IS NOT NULL
        GROUP BY 1 ORDER BY 3 DESC LIMIT 12
    """).df()
    print("\nMonths with the most volume above the limits\n")
    if months.empty:
        print("None")
        return
    months["rows_over"] = months["rows_over"].map(count)
    months["litres_over"] = months["litres_over"].map(lambda v: f"{v:,.0f}")
    print(months.to_string(index=False))


if __name__ == "__main__":
    main()
