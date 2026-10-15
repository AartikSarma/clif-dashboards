"""Why is the reintubation rate what it is? Aggregate-only diagnostic, safe to share.

    uv run python scripts/diagnose_reintubation.py --config config/<site>.yaml

Reads a finished build's staged IMV runs and ventilator waterfall and prints
only counts and percentages: no IDs, no dates. Counts below the small-cell
threshold (11) print as "<11".

An IMV run ends at the first waterfall row whose device is not IMV, and the
reintubation measure treats that as an extubation. If the next IMV row follows
within minutes, the "extubation" is usually one stray device row charted while
the patient stayed intubated. This script shows how much of the rate those
blips explain.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clif_dash.config import load_config  # noqa: E402

SMALL_CELL = 11
GAP_BINS_HOURS = [0, 1, 2, 6, 12, 24, 48]


def count(n: int) -> str:
    return f"<{SMALL_CELL}" if 0 < n < SMALL_CELL else f"{n:,}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    stage_dir = load_config(args.config).stage_dir

    runs = pd.read_parquet(stage_dir / "runs_a.parquet").sort_values(["encounter_block", "run_start"])
    runs["next_start"] = runs.groupby("encounter_block")["run_start"].shift(-1)
    ended = runs[runs["ended_by_device_change"] & runs["next_device"].ne("trach collar")].copy()
    ended["gap_h"] = (ended["next_start"] - ended["run_end"]) / pd.Timedelta(hours=1)
    back_within_48 = ended[ended["gap_h"] <= 48]

    print(f"IMV runs ending in a move to a non-IMV device (not trach collar): {count(len(ended))}")
    print(f"  followed by IMV again within 48 h: {count(len(back_within_48))} "
          f"({len(back_within_48) / max(len(ended), 1):.0%}); death and comfort-care exclusions not applied")
    print("\nTime off IMV before IMV resumed (cumulative):")
    for hours in GAP_BINS_HOURS[1:]:
        n = int((back_within_48["gap_h"] <= hours).sum())
        print(f"  <= {hours:>2} h: {count(n):>8}  ({n / max(len(back_within_48), 1):.0%} of returns to IMV)")

    print("\nDevice charted right after IMV ended:")
    for device, n in ended["next_device"].fillna("(missing)").value_counts().items():
        print(f"  {device:<20} {count(int(n)):>8}")

    waterfall = pd.concat([pd.read_parquet(f, columns=["encounter_block", "recorded_dttm", "is_scaffold"])
                           for f in glob.glob(str(stage_dir / "waterfall" / "*.parquet"))])
    by_block = {k: g for k, g in waterfall.groupby("encounter_block")}
    charted = []
    for row in back_within_48.itertuples():
        g = by_block.get(row.encounter_block)
        if g is None:
            continue
        gap = g[(g["recorded_dttm"] >= row.run_end) & (g["recorded_dttm"] < row.next_start)]
        charted.append(int((~gap["is_scaffold"]).sum()))
    charted = pd.Series(charted)
    print("\nCharted (non-scaffold) non-IMV rows between the 'extubation' and the return to IMV:")
    for label, n in [("1 row", (charted == 1).sum()), ("2 rows", (charted == 2).sum()),
                     ("3 or more", (charted >= 3).sum())]:
        print(f"  {label:<10} {count(int(n)):>8}  ({n / max(len(charted), 1):.0%})")


if __name__ == "__main__":
    main()
