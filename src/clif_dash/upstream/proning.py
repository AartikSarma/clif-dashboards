"""Proning cohort and outcomes — Python port of CLIF_Proning_Incidence_Severe_ARF.

Source: github.com/Common-Longitudinal-ICU-data-Format/CLIF_Proning_Incidence_Severe_ARF
  00_local_CLIF_prone_incidence_cohort_identification_wCOVID.Rmd  (cohort, prone episodes, outcomes)
  02_local_CLIF_prone_incidence_global_risk_adjustment.Rmd          (expected proning)
Global coefficients: chochbe1/CLIF_prone_incidence_severeARF output/intermediate/global_coefficients.csv

Deviation (set in decisions.yaml): every eligible encounter is kept instead of one random
encounter per patient.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import metrics as M

COEF_FILE = Path(__file__).parent / "reference" / "proning_global_coefficients.csv"


def _dec(D, i):
    return next(d["value"] for d in D["metrics"]["prone_12h"]["decisions"] if d["id"] == i)


def prone_sessions(pos: pd.DataFrame, s, e) -> pd.DataFrame:
    """Prone sessions within [s, e] using the study's collapse-to-changes logic."""
    p = pos[(pos["recorded_dttm"] >= s) & (pos["recorded_dttm"] <= e)].sort_values("recorded_dttm")
    if p.empty:
        return pd.DataFrame(columns=["start", "hours"])
    # drop timestamps carrying both prone and not_prone
    both = p.groupby("recorded_dttm")["position_category"].nunique()
    p = p[~p["recorded_dttm"].isin(both[both > 1].index)]
    cat = p["position_category"].to_numpy()
    t = p["recorded_dttm"].to_numpy()
    keep = np.r_[True, cat[1:] != cat[:-1]]
    tk, ck = t[keep], cat[keep]
    out = []
    for i in range(len(ck)):
        if ck[i] != "prone":
            continue
        if i + 1 < len(ck):
            end = pd.Timestamp(tk[i + 1])
        else:
            later = t[t > tk[i]]
            end = e  # last documented position: session runs to vent end (study rule)
        out.append({"start": pd.Timestamp(tk[i]), "hours": (end - pd.Timestamp(tk[i])) / pd.Timedelta(hours=1)})
    return pd.DataFrame(out)


def compute(T, ctx, D, log=print, expected=True):
    ep, enc, w, adt = ctx["ep"], ctx["enc"], ctx["w"], ctx["adt"]
    pf_thr, peep_min, fio2_min = _dec(D, "pf_threshold"), _dec(D, "peep_min"), _dec(D, "fio2_min")
    first_win = _dec(D, "first_abg_window_h")
    c_lo, c_hi = _dec(D, "confirm_window_h")
    or_h = _dec(D, "exclude_or_hours")
    funnel = []

    labs = T.get("labs", pd.DataFrame())
    if labs.empty:
        return pd.DataFrame(), [{"step": "Labs table missing — proning metrics unavailable", "n": 0}], {}
    abg = labs[labs["lab_category"].eq("po2_arterial")].merge(ctx["mapping"], on="hospitalization_id")
    abg = abg.assign(t=abg["lab_result_dttm"].fillna(abg["lab_collect_dttm"]),
                     pao2=pd.to_numeric(abg["lab_value_numeric"], errors="coerce")).dropna(subset=["pao2", "t"])
    pos = T.get("position", pd.DataFrame())
    pos = pos.merge(ctx["mapping"], on="hospitalization_id") if not pos.empty else pos
    pos_by = {k: g for k, g in pos.groupby("encounter_block")} if not pos.empty else {}
    w_by = {k: g for k, g in w.groupby("encounter_block")}
    adt_by = {k: g for k, g in adt.groupby("encounter_block")}
    enc_i = enc.set_index("encounter_block")

    first_ep = ep[ep["episode_seq"] == 1]
    funnel.append({"step": "First IMV episode of adult ICU encounters", "n": int(len(first_ep))})
    rows = []
    n_out = n_trach = n_or = n_not = n_before = 0
    for e in first_ep.itertuples():
        blk = e.encounter_block
        if getattr(e, "outside_intubation", False):
            n_out += 1
            continue
        g = w_by[blk]
        tr = g[(g["recorded_dttm"] < e.start + pd.Timedelta(hours=24)) & (g["tracheostomy"] == 1)] if "tracheostomy" in g else g.iloc[0:0]
        if len(tr):
            n_trach += 1
            continue
        a = abg[(abg["encounter_block"] == blk) & (abg["t"] >= e.start) & (abg["t"] <= e.end)].sort_values("t")
        if a.empty:
            n_not += 1
            continue
        ws = g[["recorded_dttm", "fio2_set", "peep_set"]].sort_values("recorded_dttm")
        a = pd.merge_asof(a.sort_values("t"), ws, left_on="t", right_on="recorded_dttm", direction="backward")
        a["pf"] = a["pao2"] / a["fio2_set"]
        a["q"] = (a["pf"] < pf_thr) & (a["peep_set"] >= peep_min) & (a["fio2_set"] >= fio2_min)
        q1 = a[a["q"] & (a["t"] <= e.start + pd.Timedelta(hours=first_win))]
        if q1.empty:
            n_not += 1
            continue
        t1 = q1["t"].iloc[0]
        q2 = a[a["q"] & (a["t"] > t1 + pd.Timedelta(hours=c_lo)) & (a["t"] < t1 + pd.Timedelta(hours=c_hi))]
        t2 = q2["t"].iloc[0] if len(q2) else None
        sess = prone_sessions(pos_by[blk], e.start, e.end) if blk in pos_by else pd.DataFrame(columns=["start", "hours"])
        tp = sess["start"].min() if len(sess) else None
        by_prone = tp is not None and t1 <= tp <= t1 + pd.Timedelta(hours=24)
        if t2 is None and not by_prone:
            n_not += 1
            continue
        cands = [x for x in (t2, tp if tp is not None and tp >= t1 else None) if x is not None]
        t_enr = min(cands)
        if tp is not None and tp < t1:
            n_before += 1
            continue
        ad = adt_by.get(blk)
        if ad is not None:
            recent_or = ad[ad["location_category"].str.lower().eq("procedural") &
                           (ad["in_dttm"] <= t_enr) &
                           (ad["out_dttm"].fillna(t_enr) >= t_enr - pd.Timedelta(hours=or_h))]
            if len(recent_or):
                n_or += 1
                continue
        E = enc_i.loc[blk]
        dt = (tp - t_enr) / pd.Timedelta(hours=1) if tp is not None else None
        # Covariates in the 24 h before enrollment (study script 00)
        win0 = t_enr - pd.Timedelta(hours=24)
        sw = M.sofa_window(ctx["labs_by"].get(blk), ctx["vit_by"].get(blk), ctx["pa_by"].get(blk),
                           ctx["iv"].get(blk), win0, t_enr)
        minpf = a[(a["t"] >= win0) & (a["t"] <= t_enr)]["pf"].min()
        if pd.isna(minpf):
            minpf = q1["pf"].iloc[0]
        h = ctx["heights"].loc[blk]["height_cm"] if blk in ctx["heights"].index else np.nan
        wt = ctx["weights"].get(blk, np.nan)
        bmi = wt / ((h / 100) ** 2) if not (pd.isna(h) or pd.isna(wt)) else np.nan
        rows.append({
            "episode_id": e.episode_id, "encounter_block": blk, "t_enroll": t_enr,
            "t_first_abg": t1, "eligible_by": "second ABG" if (t2 is not None and (tp is None or t2 <= tp)) else "proned ≤ 24 h",
            "proned": int(tp is not None), "p12": int(dt is not None and dt <= 12), "p24": int(dt is not None and dt <= 24),
            "p72": int(dt is not None and dt <= 72), "hours_to_prone": round(dt, 1) if dt is not None else None,
            "n_sessions": int(len(sess)),
            "first_session_h": round(float(sess["hours"].iloc[0]), 1) if len(sess) else None,
            "median_session_h": round(float(sess["hours"].median()), 1) if len(sess) else None,
            "sofa": sw["sofa"], "nee_cat": 0 if sw["nee_max"] == 0 else 1 if sw["nee_max"] <= 0.1 else 2,
            "min_pf": round(float(minpf), 1), "bmi": round(float(bmi), 1) if not pd.isna(bmi) else None,
            "age": E["age"], "female": int(str(E["sex_category"]).lower().startswith("f")),
            "ett_to_enroll_h": round((t_enr - e.start) / pd.Timedelta(hours=1), 1),
        })
    funnel += [
        {"step": "Excluded: intubated at outside hospital", "n": -n_out},
        {"step": "Excluded: tracheostomy in first 24 h of IMV", "n": -n_trach},
        {"step": "Did not meet PROSEVA ABG criteria", "n": -n_not},
        {"step": "Excluded: proned before first qualifying ABG", "n": -n_before},
        {"step": "Excluded: procedural area within 48 h before enrollment", "n": -n_or},
    ]
    pr = pd.DataFrame(rows)
    funnel.append({"step": "Proning cohort", "n": int(len(pr))})
    if not expected:
        return pr, funnel, {}
    pr, info = expected_proning(pr)
    log(f"  - proning cohort: {len(pr)} encounters")
    return pr, funnel, info


def expected_proning(pr):
    """clif_dashboards: split out of compute(); BMI imputation needs the whole cohort."""
    info = {}
    if len(pr):
        c = pd.read_csv(COEF_FILE).iloc[0]
        bmi = pr["bmi"].fillna(pr["bmi"].median() if pr["bmi"].notna().any() else 28.0)
        lo = (c["coef_(Intercept)"] + (pr["age"] - 60) / 10 * c["coef_age_scale"] + pr["female"] * c["coef_female"]
              + bmi * c["coef_bmi"] + (pr["nee_cat"] == 1) * c["coef_factor(nee_pressor_dose)1"]
              + (pr["nee_cat"] == 2) * c["coef_factor(nee_pressor_dose)2"]
              + (pr["sofa"] - 9) * c["coef_sofa_score_scale"] + (pr["min_pf"] - 80) / 10 * c["coef_min_pf_ratio_scale"])
        pr["p12_expected"] = 1 / (1 + np.exp(-lo))
        info = {"coefficients": {k: round(float(v), 5) for k, v in c.items() if k.startswith("coef")},
                "bmi_imputed": int(pr["bmi"].isna().sum())}
    return pr, info
