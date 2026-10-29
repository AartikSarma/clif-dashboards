"""An aggregate export serves the same numbers as the patient-level build it came from.

Ratios, rates, observed/expected, means, control limits and heatmaps are exact;
medians and quartiles come from histograms whose bins are about 1.2% wide, so they
agree to within a bin. The export itself holds no IDs, dates or patient rows.
"""
from __future__ import annotations

import json
import math

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from clif_dash.build import build
from clif_dash.dashboards import FLOW, GLYCEMIC, KIDNEY, LIBERATION, MICROBIOLOGY, RESPIRATORY, STEWARDSHIP
from clif_dash.export import ALL, aggregate_config, export
from clif_dash.server.app import create_app

DASHBOARDS = [RESPIRATORY, STEWARDSHIP, MICROBIOLOGY, FLOW, GLYCEMIC, LIBERATION, KIDNEY]
APPROXIMATE = 0.025       # quartiles from the histogram: within a bin, with room for interpolation


@pytest.fixture(scope="module")
def servers(built, tmp_path_factory):
    cfg, _, _ = built
    for dashboard in DASHBOARDS[1:]:
        build(cfg, dashboard)
    # Every filter, so every page can be compared with the patient-level server
    folder = tmp_path_factory.mktemp("aggregate_export")
    assert list(export(cfg, DASHBOARDS, folder, ALL)) == [d.id for d in DASHBOARDS]
    return (TestClient(create_app(cfg, DASHBOARDS)), TestClient(create_app(aggregate_config(folder), DASHBOARDS)),
            folder)


def close(ours, theirs, relative):
    if not isinstance(ours, (int, float)) or not isinstance(theirs, (int, float)):
        return ours == theirs
    return math.isclose(ours, theirs, rel_tol=relative, abs_tol=1e-3)


def assert_same(patient: dict, aggregate: dict, kind: str, where: str):
    """One aggregate from each server: exact, except quartiles (and medians) from the histogram."""
    for key, value in patient.items():
        approximate = key in ("q1", "q3") or (kind == "median" and key == "value")
        assert close(aggregate.get(key), value, APPROXIMATE if approximate else 1e-6), (where, key, aggregate, patient)


def filter_sets(client: TestClient, dashboard_id: str) -> list[dict]:
    options = client.get(f"/api/{dashboard_id}/meta").json()
    months = options["meta"]["months"]
    units = options["filter_options"].get("unit", [])
    return [{}, {"unit": units[0], "sex": "Female"} if units else {"sex": "Female"},
            {"from": months[len(months) // 3], "to": months[2 * len(months) // 3], "age_band": "65–79"}]


@pytest.mark.parametrize("dashboard", DASHBOARDS, ids=[d.id for d in DASHBOARDS])
def test_every_page_matches_the_patient_level_build(servers, dashboard):
    patient, aggregate, _ = servers
    base = f"/api/{dashboard.id}"
    kinds = {m.id: m.kind for m in dashboard.metrics}
    for params in filter_sets(patient, dashboard.id):
        assert aggregate.get(f"{base}/count", params=params).json() == patient.get(f"{base}/count", params=params).json()
        ours, theirs = (c.get(f"{base}/overview", params=params).json() for c in (aggregate, patient))
        for metric_id, tile in theirs["tiles"].items():
            assert_same(tile["agg"], ours["tiles"][metric_id]["agg"], kinds[metric_id], (params, metric_id))
            for mine, point in zip(ours["tiles"][metric_id]["series"], tile["series"]):
                assert_same(point, mine, kinds[metric_id], (params, metric_id, point["month"]))
        ours, theirs = (c.get(f"{base}/units", params=params).json() for c in (aggregate, patient))
        assert ours["groups"] == theirs["groups"]
        for mine, row in zip(ours["rows"], theirs["rows"]):
            for group, cell in row["cells"].items():
                assert_same(cell, mine["cells"][group], kinds[row["id"]], (params, row["id"], group))
        for metric in dashboard.metrics:
            ours, theirs = (c.get(f"{base}/metric/{metric.id}", params={**params, "stratum": "age_band"}).json()
                            for c in (aggregate, patient))
            assert ours["months"] == theirs["months"]
            assert (ours["control"] is None) == (theirs["control"] is None)
            if theirs["control"]:
                assert ours["control"] == pytest.approx(theirs["control"])
            # Groups with equal n can come in either order, so match them by name
            mine = {g["key"]: g for g in ours["strata"]}
            assert set(mine) == {g["key"] for g in theirs["strata"]}
            for group in theirs["strata"]:
                assert_same(group, mine[group["key"]], metric.kind, (params, metric.id, group["key"]))
        for heatmap in dashboard.heatmaps:
            for layout in ("month", "weekday"):
                ours, theirs = (c.get(f"{base}/heatmap/{heatmap.id}", params={**params, "by": layout}).json()
                                for c in (aggregate, patient))
                assert ours == theirs, (params, heatmap.id, layout)
        ours, theirs = (c.get(f"{base}/quality", params=params).json() for c in (aggregate, patient))
        assert ours == theirs


def test_the_export_holds_no_patient_rows(servers):
    _, aggregate, folder = servers
    for path in folder.glob("*/*.parquet"):
        table = pd.read_parquet(path)
        assert not {"encounter_block", "label_id", "hospitalization_id", "patient_id"} & set(table.columns), path
        assert not [c for c in table.columns if isinstance(table[c].dtype, pd.DatetimeTZDtype)], path
    for path in folder.glob("*/summary.json"):
        meta = json.loads(path.read_text())["meta"]
        assert "data_folder" not in meta and not [k for k in meta if k.endswith("_records")], path
    assert not list(folder.glob("*/stage")) and not list(folder.glob("*/mapping.parquet"))
    meta = aggregate.get("/api/respiratory/meta").json()["dashboard"]
    assert meta["aggregate"] is True and meta["patient_level"] is False and meta["detail"] is False
    for path in ("/api/respiratory/rows", "/api/respiratory/rows.csv", "/api/respiratory/detail/x"):
        assert aggregate.get(path).status_code == 404, path


def test_an_export_cannot_be_served_with_patient_rows(servers):
    _, _, folder = servers
    forced = TestClient(create_app(aggregate_config(folder), [RESPIRATORY], patient_level=True))
    assert forced.get("/api/respiratory/rows").status_code == 404


def test_the_default_export_is_split_only_by_month_hospital_and_icu(built, servers, tmp_path):
    cfg, _, _ = built
    patient, _, _ = servers
    reports = export(cfg, DASHBOARDS, tmp_path)
    assert reports["respiratory"]["episodes"]["rows"] == 1295
    for path in tmp_path.glob("*/*.parquet"):
        assert not {"sex", "race_eth", "age_band", "code_status", "trach"} & set(pd.read_parquet(path).columns), path
    coarse = TestClient(create_app(aggregate_config(tmp_path), DASHBOARDS))
    meta = coarse.get("/api/flow/meta").json()
    assert [f[0] for f in meta["filter_fields"]] == ["hospital", "unit"]
    assert [s["id"] for s in meta["strata"]] == ["unit", "hospital"]
    assert coarse.get("/api/flow/metric/icu_readmit_48h", params={"stratum": "age_band"}).status_code == 400
    unit = meta["filter_options"]["unit"][0]
    for path in ("/api/flow/overview", "/api/stewardship/overview", "/api/respiratory/overview"):
        ours, theirs = (c.get(path, params={"unit": unit}).json() for c in (coarse, patient))
        for metric_id, tile in theirs["tiles"].items():
            assert_same(tile["agg"], ours["tiles"][metric_id]["agg"], "median", (path, metric_id))
