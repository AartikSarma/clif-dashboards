"""Site configuration: where the CLIF tables live, where built outputs go, and
which decision files define the measures."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import yaml

PLATFORM_DECISIONS = "platform"
# What a site config without a `dashboards:` list builds and serves
DEFAULT_DASHBOARDS = ("respiratory", "stewardship", "microbiology", "fluids", "flow", "glycemic", "liberation", "kidney")
# Decisions the code no longer reads; a site copy written before their removal may still list them
RETIRED_DECISIONS = {"small_cell_n"}
# Where the single decisions file lived before it was split into config/decisions/
SHIPPED_LEGACY_DECISIONS = Path(__file__).resolve().parents[2] / "config" / "decisions.yaml"


@dataclass(frozen=True)
class SiteConfig:
    clif_dir: Path
    file_prefix: str
    site_name: str
    site_timezone: str
    start_date: str | None
    end_date: str | None
    decisions_dir: Path                 # holds platform.yaml and one <dashboard>.yaml per dashboard
    decision_files: dict[str, Path]     # per-file overrides from the site YAML, by name
    output_dir: Path
    encounters_per_chunk: int
    cohort_encounters_per_chunk: int
    workers: int
    dashboards: tuple[str, ...] = DEFAULT_DASHBOARDS  # dashboards this site builds and serves
    dashboard: str | None = None                      # set by bind(): the dashboard being built or served

    def table_path(self, table: str) -> Path:
        return self.clif_dir / f"{self.file_prefix}{table}.parquet"

    def bind(self, dashboard: str) -> "SiteConfig":
        """This config for one dashboard, whose outputs live in output_dir/<dashboard>."""
        return replace(self, dashboard=dashboard)

    @property
    def dashboard_dir(self) -> Path:
        if self.dashboard is None:
            raise ValueError("dashboard_dir needs a config bound to a dashboard (SiteConfig.bind)")
        return self.output_dir / self.dashboard

    @property
    def stage_dir(self) -> Path:
        return self.dashboard_dir / "stage"

    def decisions_path(self, name: str) -> Path:
        return self.decision_files.get(name, self.decisions_dir / f"{name}.yaml")

    def decisions(self) -> dict:
        """Site-wide decisions, followed by the bound dashboard's own when there is one."""
        platform = yaml.safe_load(self.decisions_path(PLATFORM_DECISIONS).read_text())
        if self.dashboard is None:
            return platform
        own_path = self.decisions_path(self.dashboard)
        return merge_decisions(platform, yaml.safe_load(own_path.read_text()),
                               self.decisions_path(PLATFORM_DECISIONS), own_path)


def merge_decisions(platform: dict, own: dict, platform_path: Path, own_path: Path) -> dict:
    """A dashboard's decisions with the site-wide ones first under `shared:`.

    A dashboard file written before the split repeats the site-wide entries; they
    are dropped when their value matches platform.yaml. A different value is an
    error, because encounters are stitched once for every dashboard.
    """
    platform_values = {d["id"]: d["value"] for d in platform["shared"]}
    own_shared = []
    for decision in own.get("shared", []):
        if decision["id"] in RETIRED_DECISIONS:
            continue
        if decision["id"] not in platform_values:
            own_shared.append(decision)
        elif decision["value"] != platform_values[decision["id"]]:
            raise ValueError(
                f"{own_path} sets the site-wide decision '{decision['id']}' to {decision['value']!r}, "
                f"but {platform_path} sets it to {platform_values[decision['id']]!r}. Site-wide decisions "
                f"apply to every dashboard: change the value in {platform_path} and remove it from {own_path}.")
    return {**own, "shared": platform["shared"] + own_shared,
            "changelog": sorted(platform.get("changelog", []) + own.get("changelog", []),
                                key=lambda entry: str(entry.get("date", "")))}


def decision_overrides(raw: dict, resolve) -> dict[str, Path]:
    """Per-file overrides from the site YAML's `decisions:` map, plus the old `decisions_file` key.

    `decisions_file` predates the split and named the respiratory dashboard's whole
    file. An existing file still serves as the respiratory file. A missing one that
    was the shipped config/decisions.yaml now means the defaults; any other missing
    path is an error.
    """
    overrides = {name: resolve(path) for name, path in (raw.get("decisions") or {}).items()}
    if raw.get("decisions_file"):
        legacy = resolve(raw["decisions_file"])
        if legacy.exists():
            overrides.setdefault("respiratory", legacy)
        elif legacy == SHIPPED_LEGACY_DECISIONS:
            print(f"Note: {legacy} was split into config/decisions/; using the files there. "
                  "Remove decisions_file from the site config to silence this note.", flush=True)
        else:
            raise FileNotFoundError(f"decisions_file {legacy} not found")
    return overrides


def load_config(config_path: str | Path, clif_dir_override: str | None = None) -> SiteConfig:
    """Read a site YAML. Relative paths resolve from the folder the YAML lives in."""
    config_path = Path(config_path).resolve()
    raw = yaml.safe_load(config_path.read_text())
    base = config_path.parent

    def resolve(value) -> Path:
        path = Path(str(value)).expanduser()
        return (path if path.is_absolute() else base / path).resolve()

    if raw.get("filetype", "parquet") != "parquet":
        raise ValueError("Only parquet CLIF tables are supported (filetype: parquet).")
    return SiteConfig(
        clif_dir=resolve(clif_dir_override or raw["clif_dir"]),
        file_prefix=raw.get("file_prefix", "clif_"),
        site_name=raw.get("site_name", "CLIF site"),
        site_timezone=raw.get("site_timezone", "UTC"),
        start_date=str(raw["start_date"]) if raw.get("start_date") else None,
        end_date=str(raw["end_date"]) if raw.get("end_date") else None,
        decisions_dir=resolve(raw.get("decisions_dir", "decisions")),
        decision_files=decision_overrides(raw, resolve),
        output_dir=resolve(raw["output_dir"]),
        encounters_per_chunk=int(raw.get("encounters_per_chunk", 2000)),
        cohort_encounters_per_chunk=int(raw.get("cohort_encounters_per_chunk", 300)),
        workers=int(raw.get("workers", 4)),
        dashboards=tuple(raw.get("dashboards") or DEFAULT_DASHBOARDS),
    )
