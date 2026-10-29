"""Two dashboards build into one site folder and serve side by side.

The second is stewardship, whose rows lack every respiratory column (code
status, tracheostomy, completeness flags, a proning table), so it fails
wherever the server still assumes them.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from clif_dash.build import build
from clif_dash.dashboards import FLOW, GLYCEMIC, KIDNEY, LIBERATION, MICROBIOLOGY, RESPIRATORY, STEWARDSHIP
from clif_dash.server.app import create_app


@pytest.fixture(scope="module")
def client(built):
    cfg, _, _ = built
    build(cfg, STEWARDSHIP)
    return TestClient(create_app(cfg, [RESPIRATORY, STEWARDSHIP], patient_level=True))


@pytest.fixture(scope="module")
def stay_months(client, built):
    cfg, _, _ = built
    return pd.read_parquet(cfg.bind("stewardship").dashboard_dir / "stay_months.parquet")


def test_both_dashboards_are_listed(client):
    listed = client.get("/api/dashboards").json()
    assert [d["id"] for d in listed] == ["respiratory", "stewardship"]


def test_every_page_of_the_second_dashboard_answers(client, stay_months):
    meta = client.get("/api/stewardship/meta").json()
    assert meta["dashboard"] == {"id": "stewardship", "title": STEWARDSHIP.title, "detail": False, "patient_level": True,
                                 "aggregate": False}
    assert [f[0] for f in meta["filter_fields"]] == ["hospital", "unit", "age_band", "sex", "race_eth"]
    assert [d["id"] for d in meta["decisions"]["shared"]][-1] == "control_limits"
    overview = client.get("/api/stewardship/overview").json()
    assert overview["n_stay_months"] == meta["n_total"] == len(stay_months)
    assert overview["tiles"]["antibacterial_days_share"]["agg"]["den"] == stay_months["days_present"].sum()
    for path in ("/api/stewardship/metric/cdi_risk", "/api/stewardship/units", "/api/stewardship/quality",
                 "/api/stewardship/rows.csv"):
        assert client.get(path).status_code == 200, path
    listed = client.get("/api/stewardship/rows", params={"metric": "resistant_gram_positive", "field": "missed",
                                                         "value": "1", "limit": 1000}).json()
    assert 0 < listed["total"] < meta["n_total"]
    assert all(row["dot_resistant_gram_positive"] > 0 for row in listed["rows"])


def test_filters_and_strata_follow_each_dashboard(client):
    unit = client.get("/api/stewardship/meta").json()["filter_options"]["unit"][0]
    everything = client.get("/api/stewardship/overview").json()["n_stay_months"]
    one_unit = client.get("/api/stewardship/overview", params={"unit": unit}).json()["n_stay_months"]
    assert 0 < one_unit < everything
    assert client.get("/api/stewardship/metric/cdi_risk", params={"stratum": "code_status"}).status_code == 400
    assert client.get("/api/stewardship/detail/anything").status_code == 404
    respiratory = client.get("/api/respiratory/meta").json()
    assert "code_status" in [f[0] for f in respiratory["filter_fields"]]


def test_a_rate_measure_pools_days_of_therapy(client, stay_months):
    page = client.get("/api/stewardship/metric/all_antibacterial").json()
    assert page["metric"]["kind"] == "rate" and page["metric"]["scale"] == 1000
    expected = 1000 * stay_months["dot_all_antibacterial"].sum() / stay_months["days_present"].sum()
    assert page["agg"]["value"] == pytest.approx(expected, abs=1e-4)  # responses round to 4 places
    assert page["control"] is None   # days of therapy cluster within patients


def test_each_dashboard_declares_its_own_filters(client):
    respiratory = client.get("/api/respiratory/meta").json()["filter_fields"]
    assert respiratory[-2:] == [["code_status", "Code status at IMV start", "values"],
                                ["trach", "Tracheostomy during episode", "flag"]]
    everything = client.get("/api/respiratory/overview").json()
    with_trach = client.get("/api/respiratory/overview", params={"trach": "yes"}).json()
    without = client.get("/api/respiratory/overview", params={"trach": "no"}).json()
    assert with_trach["n_episodes"] + without["n_episodes"] == everything["n_episodes"]
    assert with_trach["n_proning"] == everything["n_proning"]   # proning rows carry no tracheostomy flag
    stewardship = client.get("/api/stewardship/meta").json()["filter_fields"]
    assert "code_status" not in [f[0] for f in stewardship] and "trach" not in [f[0] for f in stewardship]
    # A filter the dashboard does not declare is ignored
    assert client.get("/api/stewardship/overview", params={"trach": "yes"}).json()["n_stay_months"] == \
        client.get("/api/stewardship/overview").json()["n_stay_months"]


def test_a_measure_variant_is_chosen_in_the_route(client):
    meta = client.get("/api/respiratory/meta").json()
    built = meta["meta"]["ltvv_thresholds"]
    page = client.get("/api/respiratory/metric/ltvv@7").json()
    assert page["route_id"] == "ltvv@7" and page["metric"]["numerator"] == "ltvv_num_7"
    assert [t["threshold"] for t in page["thresholds"]] == built
    default = page["default_threshold"]
    for unknown in ("ltvv@99", "ltvv@seven", "ltvv"):
        assert client.get(f"/api/respiratory/metric/{unknown}").json()["threshold"] == default, unknown
    assert client.get("/api/respiratory/metric/plateau").json()["thresholds"] == []


def shares(rows, numerator, denominator):
    return [row[numerator] / row[denominator] if row[denominator] else None for row in rows]


def test_the_list_sorts_by_each_rows_share_of_a_measure(client):
    rows = client.get("/api/respiratory/rows", params={"sort": "plateau", "direction": 1, "limit": 1000}).json()["rows"]
    values = [v for v in shares(rows, "plat_num", "plat_den") if v is not None]
    assert values == sorted(values) and len(values) > 100
    listed = client.get("/api/respiratory/rows", params={"metric": "ltvv@7", "sort": "ltvv", "direction": -1,
                                                          "limit": 1000}).json()
    assert listed["numerator_column"] == "ltvv_num_7" and listed["threshold"] == 7
    values = [v for v in shares(listed["rows"], "ltvv_num_7", "ltvv_den") if v is not None]
    assert values == sorted(values, reverse=True)


def test_a_build_older_than_its_measures_is_refused(built, tmp_path):
    cfg, _, _ = built
    build(cfg, STEWARDSHIP)
    old = replace(cfg, output_dir=tmp_path)
    shutil.copytree(cfg.bind("stewardship").dashboard_dir, old.bind("stewardship").dashboard_dir,
                    ignore=shutil.ignore_patterns("stage"))
    summary_path = old.bind("stewardship").dashboard_dir / "summary.json"
    summary = json.loads(summary_path.read_text())
    del summary["decisions"]["metrics"]["cdi_risk"]
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(RuntimeError, match="cdi_risk"):
        create_app(old, [STEWARDSHIP])


def test_every_measure_page_of_every_dashboard_answers(built):
    """Measures on secondary tables (antibiotic starts, isolates) included: each page has a funnel."""
    cfg, _, _ = built
    dashboards = [RESPIRATORY, STEWARDSHIP, MICROBIOLOGY, FLOW, GLYCEMIC, LIBERATION, KIDNEY]
    for dashboard in dashboards[1:]:
        build(cfg, dashboard)
    every = TestClient(create_app(cfg, dashboards, patient_level=True))
    for dashboard in dashboards:
        for metric in dashboard.metrics:
            response = every.get(f"/api/{dashboard.id}/metric/{metric.id}")
            assert response.status_code == 200, (dashboard.id, metric.id)
            assert response.json()["funnel"], (dashboard.id, metric.id)
            # The synthetic data leaves proning empty and has no influenza or other respiratory virus tests
            if dashboard is not RESPIRATORY and response.json()["agg"].get("den") != 0:
                assert response.json()["agg"]["value"] is not None, (dashboard.id, metric.id)
        for page in ("overview", "units", "quality", "rows"):
            assert every.get(f"/api/{dashboard.id}/{page}").status_code == 200, (dashboard.id, page)
        for heatmap in dashboard.heatmaps:
            response = every.get(f"/api/{dashboard.id}/heatmap/{heatmap.id}").json()
            if heatmap.kind == "events":
                assert response["n"] == sum(response["totals"]) > 0, (dashboard.id, heatmap.id)
            elif response["n"]:      # values: drugs the synthetic data never charts have none
                assert len(response["values"]) == 24 and len(response["values"][0]) == len(response["months"])


def test_each_drugs_mean_infusion_rate_lies_within_its_charted_rates(built):
    """The dose converter returns rows out of order; rates matched to the wrong drug would break this."""
    import duckdb

    cfg, _, _ = built
    output = build(cfg, LIBERATION)
    hours = pd.read_parquet(output / "sedation_hours.parquet")
    charted = duckdb.sql(f"""
        SELECT lower(med_category), min(med_dose), max(med_dose)
        FROM read_parquet('{cfg.table_path("medication_admin_continuous")}') WHERE med_dose > 0 GROUP BY 1
    """).fetchall()
    checked = 0
    for drug, low, high in charted:
        if f"{drug}_rated_hours" in hours and hours[f"{drug}_rated_hours"].sum() > 0:
            mean = hours[f"{drug}_rate_hours"].sum() / hours[f"{drug}_rated_hours"].sum()
            assert low <= mean <= high, (drug, mean, low, high)   # the synthetic data charts each drug in its target unit
            checked += 1
    assert checked >= 3



def test_only_aggregates_are_served_unless_patient_level_data_is_on(built):
    cfg, _, _ = built
    aggregate = TestClient(create_app(cfg, [RESPIRATORY]))     # the site config leaves patient_level_data off
    meta = aggregate.get("/api/respiratory/meta").json()["dashboard"]
    assert meta["patient_level"] is False and meta["detail"] is False
    for path in ("/api/respiratory/rows", "/api/respiratory/rows.csv", "/api/respiratory/detail/anything"):
        assert aggregate.get(path).status_code == 404, path
    total = aggregate.get("/api/respiratory/count").json()["total"]
    assert total == aggregate.get("/api/respiratory/meta").json()["n_total"]
    full = TestClient(create_app(cfg, [RESPIRATORY], patient_level=True))
    assert full.get("/api/respiratory/rows", params={"limit": 1}).json()["total"] == total
