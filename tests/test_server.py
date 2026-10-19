"""The API must give the numbers upstream's browser code gave, and the same timelines.

Aggregates are checked against upstream web/src/metrics.js run under node on the
built rows (tests/oracle/aggregate_with_upstream.mjs). Timelines are checked
against 26 episodes saved from the upstream build.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from clif_dash.server.app import create_app

ORACLE_DIR = Path(__file__).resolve().parent / "oracle"
TOLERANCE = 1e-6


@pytest.fixture(scope="session")
def client(built):
    cfg, _, _ = built
    return TestClient(create_app(cfg))


@pytest.fixture(scope="session")
def upstream_aggregates(built, tmp_path_factory):
    """Upstream metrics.js over this build's rows at full precision.

    test_oracle.py already shows these rows equal upstream's; the oracle file itself
    rounds every float to 4 places, which would blur tiny expected probabilities.
    """
    if shutil.which("node") is None:
        pytest.skip("node is needed to run upstream metrics.js")
    _, output, _ = built
    rows = {name: json.loads(pd.read_parquet(output / f"{name}.parquet").to_json(orient="records"))
            for name in ("episodes", "proning")}
    rows_file = tmp_path_factory.mktemp("node") / "rows.json"
    rows_file.write_text(json.dumps(rows))
    result = subprocess.run(["node", ORACLE_DIR / "aggregate_with_upstream.mjs", rows_file],
                            capture_output=True, text=True, check=True, cwd=ORACLE_DIR)
    return json.loads(result.stdout)


def close(ours, theirs, path=""):
    """Compare API output (rounded to 4 places) with unrounded JS output."""
    if isinstance(theirs, dict):
        return all(close(ours.get(k), v, f"{path}.{k}") for k, v in theirs.items() if v is not None or k in ours)
    if isinstance(theirs, list):
        return len(ours) == len(theirs) and all(close(a, b, f"{path}[{i}]") for i, (a, b) in enumerate(zip(ours, theirs)))
    if isinstance(theirs, (int, float)) and not isinstance(theirs, bool):
        ok = ours is not None and math.isclose(ours, theirs, rel_tol=TOLERANCE, abs_tol=1e-4)
        assert ok, f"{path}: API {ours} vs upstream {theirs}"
        return ok
    assert ours == theirs, f"{path}: API {ours!r} vs upstream {theirs!r}"
    return True


def test_overview_matches_upstream_aggregation(client, upstream_aggregates):
    tiles = client.get("/api/respiratory/overview").json()["tiles"]
    for metric_id, expected in upstream_aggregates.items():
        close(tiles[metric_id]["agg"], expected["agg"], f"{metric_id}.agg")
        close(tiles[metric_id]["series"], expected["series"], f"{metric_id}.series")


def test_unit_comparison_matches_upstream_aggregation(client, upstream_aggregates):
    table = {row["id"]: row for row in client.get("/api/respiratory/units").json()["rows"]}
    for metric_id, expected in upstream_aggregates.items():
        for unit, cell in expected["units"].items():
            ours = {k: v for k, v in table[metric_id]["cells"][unit].items() if k != "key"}
            close(ours, cell, f"{metric_id}.{unit}")


def test_control_limits_match_upstream(client, upstream_aggregates):
    for metric_id in ("height_doc", "mortality", "trach"):
        page = client.get(f"/api/respiratory/metric/{metric_id}").json()
        # The metric page charts only months that have rows; upstream's overview list includes empty months.
        expected = upstream_aggregates[metric_id]["control"]
        assert math.isclose(page["control"]["center"], expected["center"], abs_tol=1e-4)


def test_timelines_match_upstream(client):
    expected = json.loads((ORACLE_DIR / "upstream_synth10k_timelines.json").read_text())
    for episode_id, series in expected.items():
        response = client.get(f"/api/respiratory/detail/{episode_id}")
        assert response.status_code == 200, episode_id
        assert response.json()["series"] == series, episode_id


def test_filters_narrow_every_page(client):
    unit = client.get("/api/respiratory/meta").json()["filter_options"]["unit"][0]
    everything = client.get("/api/respiratory/overview").json()
    one_unit = client.get("/api/respiratory/overview", params={"unit": unit}).json()
    assert 0 < one_unit["n_episodes"] < everything["n_episodes"]
    listed = client.get("/api/respiratory/rows", params={"unit": unit, "limit": 1000}).json()
    assert listed["total"] == one_unit["n_episodes"]
    assert {row["unit"] for row in listed["rows"]} == {unit}


def test_missed_list_uses_the_measure_rule(client):
    listed = client.get("/api/respiratory/rows", params={"metric": "reintubation", "field": "missed", "value": "1",
                                                 "limit": 1000}).json()
    assert listed["total"] > 0
    assert all(row["reint_num"] > 0 for row in listed["rows"])
