"""Two dashboards build into one site folder and serve side by side.

The second is the ICU stays platform test, whose rows lack every respiratory
column (code status, tracheostomy, completeness flags, a proning table), so it
fails wherever the server still assumes them.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from clif_dash.build import build
from clif_dash.dashboards import ICU_STAYS, RESPIRATORY
from clif_dash.server.app import create_app


@pytest.fixture(scope="module")
def client(built):
    cfg, _, _ = built
    build(cfg, ICU_STAYS)
    return TestClient(create_app(cfg, [RESPIRATORY, ICU_STAYS]))


def test_both_dashboards_are_listed(client):
    listed = client.get("/api/dashboards").json()
    assert [d["id"] for d in listed] == ["respiratory", "icu_stays"]


def test_every_page_of_the_second_dashboard_answers(client):
    meta = client.get("/api/icu_stays/meta").json()
    assert meta["dashboard"] == {"id": "icu_stays", "title": ICU_STAYS.title, "detail": False}
    assert [f[0] for f in meta["filter_fields"]] == ["hospital", "unit", "age_band", "sex", "race_eth"]
    assert [d["id"] for d in meta["decisions"]["shared"]][-1] == "stay_definition"
    overview = client.get("/api/icu_stays/overview").json()
    assert overview["n_stays"] == meta["n_total"] > 0
    assert overview["tiles"]["hospital_mortality"]["agg"]["den"] == meta["n_total"]
    for path in ("/api/icu_stays/metric/icu_days", "/api/icu_stays/units", "/api/icu_stays/quality",
                 "/api/icu_stays/rows.csv"):
        assert client.get(path).status_code == 200, path
    listed = client.get("/api/icu_stays/rows", params={"metric": "icu_days", "field": "missed", "value": "1"}).json()
    assert 0 < listed["total"] < meta["n_total"]
    assert all(row["icu_days"] > 7 for row in listed["rows"])


def test_filters_and_strata_follow_each_dashboard(client):
    unit = client.get("/api/icu_stays/meta").json()["filter_options"]["unit"][0]
    everything = client.get("/api/icu_stays/overview").json()["n_stays"]
    one_unit = client.get("/api/icu_stays/overview", params={"unit": unit}).json()["n_stays"]
    assert 0 < one_unit < everything
    assert client.get("/api/icu_stays/metric/icu_days", params={"stratum": "code_status"}).status_code == 400
    assert client.get("/api/icu_stays/detail/anything").status_code == 404
    respiratory = client.get("/api/respiratory/meta").json()
    assert "code_status" in [f[0] for f in respiratory["filter_fields"]]


def test_a_rate_measure_serves_with_u_chart_limits(client, built):
    cfg, _, _ = built
    stays = pd.read_parquet(cfg.bind("icu_stays").dashboard_dir / "stays.parquet")
    page = client.get("/api/icu_stays/metric/deaths_per_icu_days").json()
    assert page["metric"]["kind"] == "rate" and page["metric"]["scale"] == 1000
    assert page["agg"]["value"] == pytest.approx(1000 * stays["died"].sum() / stays["icu_days"].sum(), abs=1e-4)  # responses round to 4 places
    assert page["control"]["kind"] == "u"
    assert client.get("/api/icu_stays/metric/hospital_mortality").json()["control"]["kind"] == "p"


def test_each_dashboard_declares_its_own_filters(client):
    respiratory = client.get("/api/respiratory/meta").json()["filter_fields"]
    assert respiratory[-2:] == [["code_status", "Code status at IMV start", "values"],
                                ["trach", "Tracheostomy during episode", "flag"]]
    everything = client.get("/api/respiratory/overview").json()
    with_trach = client.get("/api/respiratory/overview", params={"trach": "yes"}).json()
    without = client.get("/api/respiratory/overview", params={"trach": "no"}).json()
    assert with_trach["n_episodes"] + without["n_episodes"] == everything["n_episodes"]
    assert with_trach["n_proning"] == everything["n_proning"]   # proning rows carry no tracheostomy flag
    stays = client.get("/api/icu_stays/meta").json()["filter_fields"]
    assert "code_status" not in [f[0] for f in stays] and "trach" not in [f[0] for f in stays]
    # A filter the dashboard does not declare is ignored rather than applied to a column that happens to exist
    assert client.get("/api/icu_stays/overview", params={"trach": "yes"}).json()["n_stays"] == \
        client.get("/api/icu_stays/overview").json()["n_stays"]


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
    build(cfg, ICU_STAYS)
    old = replace(cfg, output_dir=tmp_path)
    shutil.copytree(cfg.bind("icu_stays").dashboard_dir, old.bind("icu_stays").dashboard_dir,
                    ignore=shutil.ignore_patterns("stage"))
    summary_path = old.bind("icu_stays").dashboard_dir / "summary.json"
    summary = json.loads(summary_path.read_text())
    del summary["decisions"]["metrics"]["deaths_per_icu_days"]
    summary_path.write_text(json.dumps(summary))
    with pytest.raises(RuntimeError, match="deaths_per_icu_days"):
        create_app(old, [ICU_STAYS])
