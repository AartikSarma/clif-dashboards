"""Decisions load in two layers: site-wide (platform.yaml), then the dashboard's own file."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from clif_dash.config import SHIPPED_LEGACY_DECISIONS, load_config

REPO = Path(__file__).resolve().parent.parent
DECISIONS = REPO / "config" / "decisions"


def site_yaml(tmp_path: Path, **extra) -> Path:
    raw = {"clif_dir": str(tmp_path), "output_dir": str(tmp_path / "out"), "decisions_dir": str(DECISIONS), **extra}
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def single_file_decisions(tmp_path: Path, **changed_values) -> Path:
    """A decisions file in the format before the split: site-wide entries inline under shared."""
    platform = yaml.safe_load((DECISIONS / "platform.yaml").read_text())
    respiratory = yaml.safe_load((DECISIONS / "respiratory.yaml").read_text())
    shared = [dict(d, value=changed_values.get(d["id"], d["value"])) for d in platform["shared"] + respiratory["shared"]]
    path = tmp_path / "decisions_my_site.yaml"
    path.write_text(yaml.safe_dump({**respiratory, "shared": shared}, allow_unicode=True))
    return path


def test_site_wide_decisions_come_first():
    cfg = load_config(REPO / "config" / "synth_10k.yaml").bind("respiratory")
    ids = [d["id"] for d in cfg.decisions()["shared"]]
    platform_ids = [d["id"] for d in yaml.safe_load((DECISIONS / "platform.yaml").read_text())["shared"]]
    assert ids[:len(platform_ids)] == platform_ids
    assert "episode_gap_hours" in ids and "metrics" in cfg.decisions()


def test_a_site_copy_from_before_the_split_still_loads(tmp_path):
    copy = single_file_decisions(tmp_path)
    cfg = load_config(site_yaml(tmp_path, decisions_file=str(copy))).bind("respiratory")
    default = load_config(site_yaml(tmp_path)).bind("respiratory")
    assert cfg.decisions_path("respiratory") == copy
    assert cfg.decisions()["shared"] == default.decisions()["shared"]


def test_a_dashboard_file_cannot_change_a_site_wide_value(tmp_path):
    copy = single_file_decisions(tmp_path, age_min=16)
    cfg = load_config(site_yaml(tmp_path, decisions_file=str(copy))).bind("respiratory")
    with pytest.raises(ValueError, match="age_min"):
        cfg.decisions()


def test_the_old_shipped_file_falls_back_to_the_defaults(tmp_path, capsys):
    assert not SHIPPED_LEGACY_DECISIONS.exists()
    cfg = load_config(site_yaml(tmp_path, decisions_file=str(SHIPPED_LEGACY_DECISIONS)))
    assert cfg.decision_files == {}
    assert "split into config/decisions" in capsys.readouterr().out


def test_any_other_missing_decisions_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(site_yaml(tmp_path, decisions_file=str(tmp_path / "nowhere.yaml")))
