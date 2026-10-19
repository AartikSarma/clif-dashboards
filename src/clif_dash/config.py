"""Site configuration: where the CLIF tables live and where built outputs go."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SiteConfig:
    clif_dir: Path
    file_prefix: str
    site_name: str
    site_timezone: str
    start_date: str | None
    end_date: str | None
    decisions_file: Path
    output_dir: Path
    encounters_per_chunk: int
    cohort_encounters_per_chunk: int
    workers: int
    dashboards: tuple[str, ...] = ("respiratory",)   # dashboards this site builds and serves
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

    def decisions(self) -> dict:
        return yaml.safe_load(self.decisions_file.read_text())


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
        decisions_file=resolve(raw.get("decisions_file", "decisions.yaml")),
        output_dir=resolve(raw["output_dir"]),
        encounters_per_chunk=int(raw.get("encounters_per_chunk", 2000)),
        cohort_encounters_per_chunk=int(raw.get("cohort_encounters_per_chunk", 300)),
        workers=int(raw.get("workers", 4)),
        dashboards=tuple(raw.get("dashboards") or ("respiratory",)),
    )
