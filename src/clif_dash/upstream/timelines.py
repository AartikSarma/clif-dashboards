"""Hourly swim-lane data for the timeline page: build_dashboard.timelines() from upstream.

Upstream embedded these series for up to 3000 episodes in the HTML; the server
calls this for one episode at a time with a context built from the staged chunk.
"""
from __future__ import annotations

import pandas as pd

from . import prep, proning


def timelines(ctx, ep, pr, tz, max_n):
    """Hourly swim-lane data for the patient timeline view."""
    w_by = {k: g for k, g in ctx["w"].groupby("encounter_block")}
    pos = ctx["T"].get("position", pd.DataFrame())
    pos = pos.merge(ctx["mapping"], on="hospitalization_id") if not pos.empty else pos
    pos_by = {k: g for k, g in pos.groupby("encounter_block")} if not pos.empty else {}
    labs = ctx["T"].get("labs", pd.DataFrame())
    abg = labs[labs["lab_category"].eq("po2_arterial")].merge(ctx["mapping"], on="hospitalization_id") if not labs.empty else labs
    abg_by = {k: g for k, g in abg.groupby("encounter_block")} if not abg.empty else {}
    out = {}
    eps = ep.sort_values("start", ascending=False).head(max_n)
    for e in eps.itertuples():
        blk = e.encounter_block
        s0 = e.start - pd.Timedelta(hours=6)
        s1 = e.end + pd.Timedelta(hours=6)
        hrs = lambda t: round((t - e.start) / pd.Timedelta(hours=1), 2)
        g = w_by[blk]
        g = g[(g["recorded_dttm"] >= s0) & (g["recorded_dttm"] <= s1)]
        sc = g[g["is_scaffold"]]
        pbw = e.pbw if e.pbw and not pd.isna(e.pbw) else None
        series = {
            "t": [hrs(t) for t in sc["recorded_dttm"]],
            "device": sc["device_category"].fillna("").tolist(),
            "mode": sc["mode_category"].fillna("").tolist(),
            "vt_pbw": [round(v / pbw, 2) if (pbw and not pd.isna(v)) else None for v in sc["tidal_volume_set"]],
            "peep": [None if pd.isna(v) else float(v) for v in sc["peep_set"]],
            "fio2": [None if pd.isna(v) else round(float(v), 2) for v in sc["fio2_set"]],
        }
        real = g[~g["is_scaffold"] & g["plateau_pressure_obs"].notna()]
        series["plateau"] = [[hrs(t), float(v)] for t, v in zip(real["recorded_dttm"], real["plateau_pressure_obs"])]
        a = abg_by.get(blk)
        pf = []
        if a is not None:
            a = a.assign(t=a["lab_result_dttm"].fillna(a["lab_collect_dttm"]))
            a = a[(a["t"] >= s0) & (a["t"] <= s1)].sort_values("t")
            if len(a):
                ws = ctx["w"][ctx["w"]["encounter_block"] == blk][["recorded_dttm", "fio2_set"]].sort_values("recorded_dttm")
                a = pd.merge_asof(a, ws, left_on="t", right_on="recorded_dttm", direction="backward")
                for t, p, f in zip(a["t"], a["lab_value_numeric"], a["fio2_set"]):
                    if not pd.isna(p) and not pd.isna(f) and f > 0:
                        pf.append([hrs(t), round(float(p) / float(f))])
        series["pf"] = pf
        pa = ctx["pa_by"].get(blk)
        if pa is not None:
            r = pa[pa["cat_l"].eq("rass") & (pa["recorded_dttm"] >= s0) & (pa["recorded_dttm"] <= s1)]
            series["rass"] = [[hrs(t), float(v)] for t, v in zip(r["recorded_dttm"], r["numerical_value"]) if not pd.isna(v)]
            ev = pa[pa["cat_l"].isin(["sat_delivery_pass_fail", "sbt_delivery_pass_fail"]) &
                    (pa["recorded_dttm"] >= s0) & (pa["recorded_dttm"] <= s1)]
            series["events"] = [[hrs(t), c.split("_")[0].upper(), str(v)] for t, c, v in
                                zip(ev["recorded_dttm"], ev["cat_l"], ev["categorical_value"])]
        else:
            series["rass"], series["events"] = [], []
        iv = ctx["iv"].get(blk)
        bands = {}
        for kind in ("sedative", "paralytic"):
            bands[kind] = [[hrs(max(a_, s0)), hrs(min(b_, s1))] for a_, b_ in prep.on_intervals(iv, kind) if b_ > s0 and a_ < s1]
        if blk in pos_by:
            ss = proning.prone_sessions(pos_by[blk], s0, s1)
            bands["prone"] = [[hrs(r.start), round(hrs(r.start) + r.hours, 2)] for r in ss.itertuples()]
        else:
            bands["prone"] = []
        series["bands"] = bands
        nee = []
        if iv is not None:
            for t in sorted(set(iv[(iv["kind"] == "vaso") & (iv["admin_dttm"] >= s0) & (iv["admin_dttm"] <= s1)]["admin_dttm"])):
                nee.append([hrs(t), round(prep.nee_at(iv, t), 3)])
        series["nee"] = nee
        series["para_days"] = [[hrs(d0), hrs(d1), h_, int(f_)] for d0, d1, h_, f_ in ctx.get("para_days", {}).get(e.episode_id, [])]
        series["end_h"] = hrs(e.end)
        out[e.episode_id] = series
    return out
