"""HTTP API for the dashboards, plus the built web UI.

Each built dashboard gets the same routes under /api/<dashboard id>/, and
/api/dashboards lists them. Every response is computed from the built outputs on
request, under the filters in the query string. Responses carry hospitalization
IDs and dates, so the server binds to localhost unless told otherwise; it has no
login yet.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import SiteConfig
from ..dashboard import Dashboard, Metric
from ..dashboards import DASHBOARDS
from . import aggregate as agg
from .store import FILTER_FIELDS, Filters, Store, jsonable

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
ROW_PAGE_SIZE = 100
# Respiratory list columns computed from two fields: sort by the share they display
DERIVED_SORT = {"ltvv": ("ltvv_num", "ltvv_den"), "plat": ("plat_num", "plat_den"), "sat": ("sat_num", "sat_den"),
                "sbt": ("sbt_num", "sbt_den"), "para8": ("para8_num", "para8_den")}


def parse_filters(request: Request) -> Filters:
    params = request.query_params
    return Filters(
        month_from=params.get("from") or None,
        month_to=params.get("to") or None,
        values={column: params.getlist(column) for column, _ in FILTER_FIELDS if params.getlist(column)},
        trach=params.get("trach") or None,
    )


def create_app(cfg: SiteConfig, dashboards: list[Dashboard] | None = None) -> FastAPI:
    """Serve every dashboard in `dashboards` (default: the site's list) that has a finished build."""
    dashboards = [DASHBOARDS[i] for i in cfg.dashboards] if dashboards is None else dashboards
    built = [d for d in dashboards if (cfg.bind(d.id).dashboard_dir / "summary.json").exists()]
    app = FastAPI(title="CLIF ICU quality dashboards", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/dashboards")
    def dashboard_list():
        return [{"id": d.id, "title": d.title} for d in built]

    for dashboard in built:
        app.include_router(dashboard_routes(cfg.bind(dashboard.id), dashboard), prefix=f"/api/{dashboard.id}")

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIST / "index.html")

    return app


def dashboard_routes(cfg: SiteConfig, dashboard: Dashboard) -> APIRouter:
    """One dashboard's API. `cfg` is bound to the dashboard."""
    store = Store(cfg, dashboard.row_tables)
    detail_builder = dashboard.detail(store) if dashboard.detail else None
    metrics, metrics_by_id, resolve = dashboard.metrics, dashboard.metrics_by_id, dashboard.resolve
    strata, completeness_flags = dashboard.strata, dashboard.completeness
    primary, id_column = store.primary.name, store.primary.id_column
    ltvv_thresholds = store.meta.get("ltvv_thresholds", [])
    router = APIRouter()

    def filtered(request: Request) -> dict[str, pd.DataFrame]:
        filters = parse_filters(request)
        return {name: filters.apply(rows) for name, rows in store.tables.items()}

    def months_of(tables: dict[str, pd.DataFrame]) -> list[str]:
        return sorted(set().union(*(set(rows["month"].dropna()) for rows in tables.values())))

    def funnel_for(source: str):
        """The primary table's cohort funnel is `funnel`; another table's is `<name>_funnel`."""
        return store.summary.get("funnel" if source == primary else f"{source}_funnel")

    @router.get("/meta")
    def meta():
        return jsonable({
            "dashboard": {"id": dashboard.id, "title": dashboard.title, "detail": detail_builder is not None},
            "row_tables": [t.public() for t in dashboard.row_tables],
            "meta": store.meta, "decisions": store.decisions, "data_quality": store.summary["data_quality"],
            "funnels": {t.name: funnel_for(t.name) for t in dashboard.row_tables},
            "metrics": [m.public() for m in metrics], "domains": dashboard.domains,
            "strata": [{"id": i, "label": label} for i, label in strata if i in store.primary_rows.columns],
            "completeness": [list(c) for c in completeness_flags],
            "filter_fields": [list(f) for f in store.filter_fields()], "filter_options": store.filter_options(),
            "trach_filter": "trach" in store.primary_rows.columns,
            "n_total": int(len(store.primary_rows)), "small_cell_n": store.small_cell_n,
        })

    @router.get("/overview")
    def overview(request: Request):
        tables = filtered(request)
        months = months_of(tables)
        tiles = {}
        for metric in metrics:
            rows = tables[metric.source]
            tiles[metric.id] = {"agg": agg.aggregate(metric, rows), "series": agg.monthly(metric, rows, months)}
        return jsonable({**{f"n_{name}": len(rows) for name, rows in tables.items()}, "months": months, "tiles": tiles})

    @router.get("/metric/{route_id}")
    def metric_page(route_id: str, request: Request, stratum: str = Query("unit")):
        if stratum not in dict(strata):
            raise HTTPException(400, f"unknown stratum {stratum}")
        tables = filtered(request)
        metric, threshold, default = resolve(route_id, store.decisions, ltvv_thresholds)
        rows = tables[metric.source]
        months = sorted(rows["month"].dropna().unique().tolist())
        series = agg.monthly(metric, rows, months)
        independent = metric.den_label in dashboard.independent_denominators
        groups = agg.by_group(metric, rows, stratum)
        top_keys = [g["key"] for g in groups[:6]]
        labels = rows[stratum].astype(object).where(rows[stratum].notna(), "Unknown") if len(rows) else rows[stratum]
        thresholds = []
        if threshold is not None:
            for t in ltvv_thresholds:
                variant, _, _ = resolve(f"ltvv@{t}", store.decisions, ltvv_thresholds)
                thresholds.append({"threshold": t, "agg": agg.aggregate(variant, rows)})
        extra = [{"key": k, **agg.aggregate(Metric(metric.id, metric.source, "ratio", k, None), rows)}
                 for k in metric.extra]
        parts = [{"key": k, "label": label, **agg.aggregate(Metric(metric.id, metric.source, "ratio", k, metric.denominator), rows)}
                 for k, label in metric.parts]
        primary_rows = tables[primary]
        completeness = [{"key": k, "label": label, "n": int(primary_rows[k].sum()), "total": int(len(primary_rows))}
                        for k, label in completeness_flags if k in metric.needs]
        return jsonable({
            "metric": metric.public(), "threshold": threshold, "default_threshold": default,
            "route_id": f"ltvv@{threshold:g}" if threshold is not None else metric.id,
            "agg": agg.aggregate(metric, rows), "months": months, "series": series,
            "control": agg.control_limits(metric, series) if independent else None,
            "thresholds": thresholds, "extra": extra, "parts": parts, "strata": groups,
            "lines": [{"key": k, "points": agg.monthly(metric, rows[labels == k], months)} for k in top_keys],
            "completeness": completeness,
            "funnel": funnel_for(metric.source),
        })

    @router.get("/units")
    def units(request: Request):
        tables = filtered(request)
        unit_names = sorted(tables[primary]["unit"].dropna().unique().tolist())
        table = []
        for metric in metrics:
            rows = tables[metric.source]
            cells = {g["key"]: g for g in agg.by_group(metric, rows, "unit", keys=unit_names)}
            table.append({"id": metric.id, "all": agg.aggregate(metric, rows), "cells": cells})
        return jsonable({"units": unit_names, "rows": table})

    def row_selection(request: Request, metric_arg: str | None, field: str | None, value: str | None,
                      search: str | None, sort: str, direction: int):
        tables = filtered(request)
        selected = tables[primary]
        note, numerator_column, threshold = None, "ltvv_num", None
        if metric_arg and metric_arg.split("@")[0] in metrics_by_id:
            metric, threshold, _ = resolve(metric_arg, store.decisions, ltvv_thresholds)
            if threshold is not None:
                numerator_column = metric.numerator
            title = store.decisions["metrics"][metric.id]["short"]
            if metric.source != primary:
                source = tables[metric.source]
                chosen = (metric.missed(source) if field == "missed"
                          else source[field].astype(object).where(source[field].notna(), "Unknown") == value)
                selected = selected[selected[id_column].isin(source.loc[chosen, id_column])]
            elif field == "missed":
                selected = selected[metric.missed(selected)]
            elif field in selected.columns:
                labels = selected[field].astype(object).where(selected[field].notna(), "Unknown")
                selected = selected[labels == value]
            note = f"{store.primary.unit_plural.capitalize()} that missed: {title}" if field == "missed" else f"{title} · {value}"
        if search:
            hits = store.search_text.reindex(selected[id_column]).str.contains(search.lower(), regex=False)
            selected = selected[hits.to_numpy()]
        if sort in DERIVED_SORT and DERIVED_SORT[sort][1] in selected.columns:
            num, den = DERIVED_SORT[sort]
            num = numerator_column if sort == "ltvv" else num
            key = selected[num] / selected[den].where(selected[den] > 0)
        elif sort in selected.columns:
            key = selected[sort]
        else:
            key = selected["start" if "start" in selected.columns else id_column]
        order = key.sort_values(ascending=direction > 0, na_position="first" if direction > 0 else "last",
                                kind="stable").index
        return selected.loc[order], note, numerator_column, threshold

    @router.get("/rows")
    def row_list(request: Request, metric: str | None = None, field: str | None = None, value: str | None = None,
                 q: str | None = None, sort: str = "start", direction: int = -1, offset: int = 0,
                 limit: int = Query(ROW_PAGE_SIZE, le=1000)):
        rows, note, numerator_column, threshold = row_selection(request, metric, field, value, q, sort, direction)
        page = store.displays[primary].loc[rows[id_column].iloc[offset:offset + limit]]
        return jsonable({"total": int(len(rows)), "offset": offset, "note": note, "ltvv_column": numerator_column,
                         "ltvv_threshold": threshold, "rows": page.to_dict("records")})

    @router.get("/rows.csv")
    def row_csv(request: Request, metric: str | None = None, field: str | None = None, value: str | None = None,
                q: str | None = None, sort: str = "start", direction: int = -1):
        rows, *_ = row_selection(request, metric, field, value, q, sort, direction)
        table = store.displays[primary].loc[rows[id_column]].drop(columns=[id_column])
        buffer = io.StringIO()
        table.to_csv(buffer, index=False, quoting=csv.QUOTE_MINIMAL)
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={dashboard.id}_{primary}.csv"})

    @router.get("/detail/{row_id}")
    def row_detail(row_id: str):
        series = detail_builder.build(row_id) if detail_builder else None
        if series is None:
            raise HTTPException(404, f"no {store.primary.unit} {row_id}")
        ids = store.displays[primary][id_column]
        position = int((ids == row_id).to_numpy().argmax())
        related = {}
        for table in dashboard.row_tables[1:]:
            match = store.displays[table.name][store.displays[table.name][id_column] == row_id]
            related[table.name] = match.iloc[0].to_dict() if len(match) else None
        return jsonable({
            "row": store.displays[primary].loc[row_id].to_dict(), "series": series, **related,
            "previous": ids.iloc[position - 1] if position > 0 else None,
            "next": ids.iloc[position + 1] if position + 1 < len(ids) else None,
        })

    @router.get("/quality")
    def quality(request: Request):
        rows = filtered(request)[primary]
        unit_names = sorted(rows["unit"].dropna().unique().tolist())
        completeness = []
        for key, label in completeness_flags:
            cells = {}
            for unit in [None, *unit_names]:
                unit_rows = rows if unit is None else rows[rows["unit"] == unit]
                cells[unit or "__all__"] = round(100 * unit_rows[key].sum() / len(unit_rows)) if len(unit_rows) else None
            completeness.append({"key": key, "label": label, "cells": cells})
        return jsonable({"units": unit_names, "completeness": completeness, "n_rows": len(rows),
                         "n_with_height": int(rows["has_height"].sum()) if "has_height" in rows.columns else None})

    return router
