"""One build of the synthetic data, shared by every test in the session.

The synthetic data and its config are local: copy config/synthetic_template.yaml
to config/synth_10k.yaml and point it at the synthetic_clif v0.7.0 release. Tests
that need them are skipped when either is missing.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from clif_dash.build import build
from clif_dash.config import load_config

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config" / "synth_10k.yaml"


@pytest.fixture(scope="session")
def synthetic_config():
    if not CONFIG.exists():
        pytest.skip(f"{CONFIG} not found: copy config/synthetic_template.yaml there and set clif_dir")
    cfg = load_config(CONFIG)
    if not cfg.clif_dir.exists():
        pytest.skip(f"synthetic CLIF data not found at {cfg.clif_dir}")
    return cfg


@pytest.fixture(scope="session")
def built(synthetic_config, tmp_path_factory):
    # The tests' saved outputs cover the respiratory dashboard; others build their own as needed
    cfg = replace(synthetic_config, output_dir=tmp_path_factory.mktemp("synth_10k_build"))
    output = build(cfg)
    return cfg, output, json.loads((output / "summary.json").read_text())
