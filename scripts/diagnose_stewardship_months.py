"""Why do the stewardship rates jump at the start or end of the data? Aggregate-only, safe to share.

    uv run python scripts/diagnose_stewardship_months.py --config config/<site>.yaml

Prints month-by-month counts for the first and last months of the site's data, beside
the median month: hospitalizations admitted and discharged, ICU stays starting and
ICU ADT rows left open, administered antimicrobial doses, and (from the last build)
days present and antibacterial days of therapy. No IDs and no dates finer than a
month. Counts below the small-cell threshold (11) print as "<11".

A run of months where doses are missing while ICU stays are not points at medication
records that begin later or end earlier than ADT. Months where admissions, discharges
or open ICU rows drift from the median point at an extract that leaves out patients
still in hospital at its end.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clif_dash.config import load_config  # noqa: E402
from clif_dash.dashboards.stewardship.build import group_agents, shared_values  # noqa: E402

SMALL_CELL = 11
EDGE_MONTHS = 6


def count(n) -> str:
    if pd.isna(n):
        return "-"
    n = int(n)
    return f"<{SMALL_CELL}" if 0 < n < SMALL_CELL else f"{n:,}"


def monthly(con, sql: str, name: str) -> pd.Series:
    frame = con.execute(sql).df()
    return frame.set_index("month")["n"].rename(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config).bind("stewardship")
    decisions = cfg.decisions()
    shared = shared_values(decisions)
    agents = ", ".join(f"'{a}'" for a in sorted(set().union(*(a for a, _ in group_agents(decisions).values()))))
    actions = ", ".join(f"'{a.lower()}'" for a in shared["administered_actions"])
    con = duckdb.connect()
    con.execute(f"SET TimeZone = '{cfg.site_timezone}'")

    def month_of(column: str) -> str:
        return f"strftime(CAST({column} AS TIMESTAMPTZ), '%Y-%m')"

    hospitalization = f"read_parquet('{cfg.table_path('hospitalization')}')"
    adt = f"read_parquet('{cfg.table_path('adt')}')"
    meds = f"read_parquet('{cfg.table_path('medication_admin_intermittent')}')"
    columns = [
        monthly(con, f"SELECT {month_of('admission_dttm')} AS month, count(*) AS n FROM {hospitalization} GROUP BY 1",
                "admitted"),
        monthly(con, f"SELECT {month_of('discharge_dttm')} AS month, count(*) AS n FROM {hospitalization} GROUP BY 1",
                "discharged"),
        monthly(con, f"SELECT {month_of('in_dttm')} AS month, count(*) AS n FROM {adt} "
                     "WHERE lower(CAST(location_category AS VARCHAR)) = 'icu' GROUP BY 1", "icu_stays_starting"),
        monthly(con, f"SELECT {month_of('in_dttm')} AS month, count(*) AS n FROM {adt} "
                     "WHERE lower(CAST(location_category AS VARCHAR)) = 'icu' AND out_dttm IS NULL GROUP BY 1",
                "icu_rows_open"),
        monthly(con, f"SELECT {month_of('admin_dttm')} AS month, count(*) AS n FROM {meds} "
                     f"WHERE lower(CAST(med_category AS VARCHAR)) IN ({agents}) "
                     f"AND lower(CAST(mar_action_group AS VARCHAR)) IN ({actions}) GROUP BY 1", "doses"),
    ]
    built = cfg.dashboard_dir / "stay_months.parquet"
    if built.exists():
        rows = pd.read_parquet(built).groupby("month")[["days_present", "dot_all_antibacterial"]].sum()
        columns += [rows["days_present"].rename("days_present"), rows["dot_all_antibacterial"].rename("antibacterial_dot")]
    table = pd.concat(columns, axis=1).sort_index()
    table = table[table.index.notna()]

    print(f"{len(table)} months with any data, {table.index.min()} to {table.index.max()} ({cfg.site_timezone})")
    if not built.exists():
        print("No stewardship build found, so days present and days of therapy are not shown.")
    edges = pd.concat([table.head(EDGE_MONTHS), table.tail(EDGE_MONTHS)])
    edges = edges[~edges.index.duplicated()]
    header = f"{'month':<9}" + "".join(f"{c:>20}" for c in table.columns)
    print("\n" + header)
    for month, row in edges.iterrows():
        print(f"{month:<9}" + "".join(f"{count(v):>20}" for v in row))
    print(f"{'median':<9}" + "".join(f"{count(v):>20}" for v in table.median()))
    if "antibacterial_dot" in table:
        rate = 1000 * table["antibacterial_dot"] / table["days_present"]
        print("\nAntibacterial days of therapy per 1,000 days present, edge months then median:")
        for month in edges.index:
            if pd.notna(rate.get(month)):
                print(f"  {month}  {rate[month]:6.0f}")
        print(f"  median   {rate.median():6.0f}")


if __name__ == "__main__":
    main()
