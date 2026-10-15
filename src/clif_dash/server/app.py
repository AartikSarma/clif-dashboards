"""HTTP API for the dashboard, plus the built web UI.

Every response is computed from the built outputs on request, under the filters
in the query string. Responses carry hospitalization IDs and dates, so the
server binds to localhost unless told otherwise; it has no login yet.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import SiteConfig
from . import aggregate as agg
from .registry import COMPLETENESS, DOMAINS, INDEPENDENT_DENOMINATORS, METRICS, METRICS_BY_ID, STRATA, Metric, resolve
from .store import FILTER_FIELDS, Filters, Store, jsonable
from .timeline import TimelineBuilder

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
EPISODE_PAGE_SIZE = 100
# Episode-list columns computed from two fields: sort by the share they display
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


def create_app(cfg: SiteConfig) -> FastAPI:
    store = Store(cfg)
    timeline_builder = TimelineBuilder(store)
    ltvv_thresholds = store.meta["ltvv_thresholds"]
    app = FastAPI(title="CLIF ICU quality dashboard", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def filtered(request: Request) -> tuple[pd.DataFrame, pd.DataFrame]:
        filters = parse_filters(request)
        return filters.apply(store.episodes), filters.apply(store.proning)

    def rows_for(metric: Metric, episodes, proning):
        return proning if metric.source == "proning" else episodes

    def months_of(*frames) -> list[str]:
        return sorted(set().union(*(set(f["month"].dropna()) for f in frames)))

    @app.get("/api/meta")
    def meta():
        return jsonable({
            "meta": store.meta, "decisions": store.decisions, "funnel": store.summary["funnel"],
            "proning_funnel": store.summary["proning_funnel"], "data_quality": store.summary["data_quality"],
            "metrics": [m.public() for m in METRICS], "domains": DOMAINS,
            "strata": [{"id": i, "label": label} for i, label in STRATA],
            "completeness": [list(c) for c in COMPLETENESS],
            "filter_fields": [list(f) for f in FILTER_FIELDS], "filter_options": store.filter_options(),
            "n_episodes_total": int(len(store.episodes)), "small_cell_n": store.small_cell_n,
        })

    @app.get("/api/overview")
    def overview(request: Request):
        episodes, proning = filtered(request)
        months = months_of(episodes, proning)
        tiles = {}
        for metric in METRICS:
            rows = rows_for(metric, episodes, proning)
            tiles[metric.id] = {"agg": agg.aggregate(metric, rows), "series": agg.monthly(metric, rows, months)}
        return jsonable({"n_episodes": len(episodes), "n_proning": len(proning), "months": months, "tiles": tiles})

    @app.get("/api/metric/{route_id}")
    def metric_page(route_id: str, request: Request, stratum: str = Query("unit")):
        if stratum not in dict(STRATA):
            raise HTTPException(400, f"unknown stratum {stratum}")
        episodes, proning = filtered(request)
        metric, threshold, default = resolve(route_id, store.decisions, ltvv_thresholds)
        rows = rows_for(metric, episodes, proning)
        months = sorted(rows["month"].dropna().unique().tolist())
        series = agg.monthly(metric, rows, months)
        independent = metric.den_label in INDEPENDENT_DENOMINATORS
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
        completeness = [{"key": k, "label": label, "n": int(episodes[k].sum()), "total": int(len(episodes))}
                        for k, label in COMPLETENESS if k in metric.needs]
        return jsonable({
            "metric": metric.public(), "threshold": threshold, "default_threshold": default,
            "route_id": f"ltvv@{threshold:g}" if threshold is not None else metric.id,
            "agg": agg.aggregate(metric, rows), "months": months, "series": series,
            "control": agg.p_chart(series) if metric.kind == "ratio" and independent else None,
            "thresholds": thresholds, "extra": extra, "parts": parts, "strata": groups,
            "lines": [{"key": k, "points": agg.monthly(metric, rows[labels == k], months)} for k in top_keys],
            "completeness": completeness,
            "funnel": store.summary["proning_funnel" if metric.source == "proning" else "funnel"],
        })

    @app.get("/api/units")
    def units(request: Request):
        episodes, proning = filtered(request)
        unit_names = sorted(episodes["unit"].dropna().unique().tolist())
        table = []
        for metric in METRICS:
            rows = rows_for(metric, episodes, proning)
            cells = {g["key"]: g for g in agg.by_group(metric, rows, "unit", keys=unit_names)}
            table.append({"id": metric.id, "all": agg.aggregate(metric, rows), "cells": cells})
        return jsonable({"units": unit_names, "rows": table})

    def episode_selection(request: Request, metric_arg: str | None, field: str | None, value: str | None,
                          search: str | None, sort: str, direction: int):
        episodes, proning = filtered(request)
        note, numerator_column, threshold = None, "ltvv_num", None
        if metric_arg and metric_arg.split("@")[0] in METRICS_BY_ID:
            metric, threshold, _ = resolve(metric_arg, store.decisions, ltvv_thresholds)
            if threshold is not None:
                numerator_column = metric.numerator
            title = store.decisions["metrics"][metric.id]["short"]
            if metric.source == "proning":
                chosen = (metric.missed(proning) if field == "missed"
                          else proning[field].astype(object).where(proning[field].notna(), "Unknown") == value)
                episodes = episodes[episodes["episode_id"].isin(proning.loc[chosen, "episode_id"])]
            elif field == "missed":
                episodes = episodes[metric.missed(episodes)]
            elif field in episodes.columns:
                labels = episodes[field].astype(object).where(episodes[field].notna(), "Unknown")
                episodes = episodes[labels == value]
            note = f"Episodes that missed: {title}" if field == "missed" else f"{title} · {value}"
        if search:
            hits = store.search_text.reindex(episodes["episode_id"]).str.contains(search.lower(), regex=False)
            episodes = episodes[hits.to_numpy()]
        if sort in DERIVED_SORT:
            num, den = DERIVED_SORT[sort]
            num = numerator_column if sort == "ltvv" else num
            key = episodes[num] / episodes[den].where(episodes[den] > 0)
        elif sort in episodes.columns:
            key = episodes[sort]
        else:
            key = episodes["start"]
        order = key.sort_values(ascending=direction > 0, na_position="first" if direction > 0 else "last",
                                kind="stable").index
        return episodes.loc[order], note, numerator_column, threshold

    @app.get("/api/episodes")
    def episode_list(request: Request, metric: str | None = None, field: str | None = None, value: str | None = None,
                     q: str | None = None, sort: str = "start", direction: int = -1, offset: int = 0,
                     limit: int = Query(EPISODE_PAGE_SIZE, le=1000)):
        rows, note, numerator_column, threshold = episode_selection(request, metric, field, value, q, sort, direction)
        page = store.episode_display.loc[rows["episode_id"].iloc[offset:offset + limit]]
        return jsonable({"total": int(len(rows)), "offset": offset, "note": note, "ltvv_column": numerator_column,
                         "ltvv_threshold": threshold, "rows": page.to_dict("records")})

    @app.get("/api/episodes.csv")
    def episode_csv(request: Request, metric: str | None = None, field: str | None = None, value: str | None = None,
                    q: str | None = None, sort: str = "start", direction: int = -1):
        rows, *_ = episode_selection(request, metric, field, value, q, sort, direction)
        table = store.episode_display.loc[rows["episode_id"]].drop(columns=["episode_id"])
        buffer = io.StringIO()
        table.to_csv(buffer, index=False, quoting=csv.QUOTE_MINIMAL)
        return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=episodes.csv"})

    @app.get("/api/timeline/{episode_id}")
    def episode_timeline(episode_id: str):
        series = timeline_builder.build(episode_id)
        if series is None:
            raise HTTPException(404, f"no episode {episode_id}")
        episode = store.episode_display.loc[episode_id].to_dict()
        proning_row = store.proning_display[store.proning_display["episode_id"] == episode_id]
        ids = store.episode_display["episode_id"]
        position = int((ids == episode_id).to_numpy().argmax())
        return jsonable({
            "episode": episode, "series": series,
            "proning": proning_row.iloc[0].to_dict() if len(proning_row) else None,
            "previous": ids.iloc[position - 1] if position > 0 else None,
            "next": ids.iloc[position + 1] if position + 1 < len(ids) else None,
        })

    @app.get("/api/quality")
    def quality(request: Request):
        episodes, _ = filtered(request)
        unit_names = sorted(episodes["unit"].dropna().unique().tolist())
        completeness = []
        for key, label in COMPLETENESS:
            cells = {}
            for unit in [None, *unit_names]:
                rows = episodes if unit is None else episodes[episodes["unit"] == unit]
                cells[unit or "__all__"] = round(100 * rows[key].sum() / len(rows)) if len(rows) else None
            completeness.append({"key": key, "label": label, "cells": cells})
        return jsonable({"units": unit_names, "completeness": completeness, "n_episodes": len(episodes),
                         "n_with_height": int(episodes["has_height"].sum())})

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIST / "index.html")

    return app
