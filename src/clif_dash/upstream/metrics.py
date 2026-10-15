"""Per-episode quality metrics (metrics 1-4, 8-15)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import prep

H = pd.Timedelta(hours=1)


def ltvv_key(t) -> str:
    """Column name for hours at or below threshold t, e.g. 6 -> ltvv_num_6, 6.5 -> ltvv_num_6_5."""
    return "ltvv_num_" + (str(int(t)) if float(t).is_integer() else str(t).replace(".", "_"))


def pbw(height_cm, sex) -> float:
    if pd.isna(height_cm):
        return np.nan
    base = 45.5 if str(sex).lower().startswith("f") else 50.0
    return base + 0.91 * (height_cm - 152.4)


def first_height(vitals: pd.DataFrame, mapping, lo, hi) -> pd.DataFrame:
    v = vitals[vitals["vital_category"].eq("height_cm")].merge(mapping, on="hospitalization_id")
    v = v[v["vital_value"].between(lo, hi)].sort_values("recorded_dttm")
    return v.groupby("encounter_block").agg(height_cm=("vital_value", "first"),
                                            height_time=("recorded_dttm", "first"))


def first_weight(vitals: pd.DataFrame, mapping) -> pd.Series:
    v = vitals[vitals["vital_category"].eq("weight_kg")].merge(mapping, on="hospitalization_id")
    v = v[v["vital_value"].between(30, 300)].sort_values("recorded_dttm")
    return v.groupby("encounter_block")["vital_value"].first()


def code_status_at(cs_by_pat: dict, patient_id, t) -> str:
    g = cs_by_pat.get(patient_id)
    if g is None:
        return "Full (none recorded)"
    g = g[g["start_dttm"] <= t]
    if g.empty:
        return "Full (none recorded)"
    c = str(g.iloc[-1]["code_status_category"])
    if c in ("Full", "Presume Full"):
        return "Full"
    if c in ("DNR", "DNAR", "UDNR"):
        return "DNR"
    if "DNI" in c:
        return "DNR/DNI"
    if c in ("AND",):
        return "Comfort care (AND)"
    return "Other"


def limited_code_between(cs_by_pat, patient_id, s, e) -> bool:
    g = cs_by_pat.get(patient_id)
    if g is None:
        return False
    g = g[(g["start_dttm"] > s) & (g["start_dttm"] <= e)]
    return bool(g["code_status_category"].astype(str).str.contains("DNR|DNI|AND|DNAR").any())


# --------------------------------------------------------------------------- #
# SOFA (study-style: platelets, bilirubin, creatinine, cardiovascular, CNS)
# --------------------------------------------------------------------------- #
def sofa_window(labs_b, vit_b, pa_b, iv_b, s, e) -> dict:
    def lab(cat, fn):
        if labs_b is None:
            return np.nan
        x = labs_b[(labs_b["lab_category"] == cat) & (labs_b["t"] >= s) & (labs_b["t"] <= e)]["lab_value_numeric"]
        return fn(x) if len(x.dropna()) else np.nan
    plt = lab("platelet_count", np.min)
    bili = lab("bilirubin_total", np.max)
    cr = lab("creatinine", np.max)
    s_plt = 0 if pd.isna(plt) else 4 if plt < 20 else 3 if plt < 50 else 2 if plt < 100 else 1 if plt < 150 else 0
    s_bili = 0 if pd.isna(bili) else 0 if bili < 1.2 else 1 if bili < 2 else 2 if bili < 6 else 3 if bili < 12 else 4
    s_cr = 0 if pd.isna(cr) else 0 if cr < 1.2 else 1 if cr < 2 else 2 if cr < 3.5 else 3 if cr < 5 else 4
    nee = prep.nee_max(iv_b, s, e)
    s_pressor = 4 if nee > 0.1 else 3 if nee > 0 else 0
    s_map = 0
    if vit_b is not None:
        m = vit_b[(vit_b["vital_category"] == "map") & (vit_b["recorded_dttm"] >= s) & (vit_b["recorded_dttm"] <= e)]["vital_value"]
        if len(m.dropna()) and m.min() < 70:
            s_map = 1
    s_cv = s_pressor if s_pressor > 1 else s_map
    s_cns = 0
    if pa_b is not None:
        p = pa_b[(pa_b["recorded_dttm"] >= s) & (pa_b["recorded_dttm"] <= e)]
        gcs = p[p["assessment_category"] == "gcs_total"]["numerical_value"].dropna()
        rass = p[p["cat_l"] == "rass"]["numerical_value"].dropna()
        sg = sr = 0
        if len(gcs):
            g = gcs.min()
            sg = 0 if g >= 15 else 1 if g >= 13 else 2 if g >= 10 else 3 if g >= 6 else 4
        if len(rass):
            r = rass.min()
            sr = 0 if r > -1 else 1 if r == -1 else 2 if r == -2 else 3 if r == -3 else 4
        s_cns = max(sg, sr)
    return {"sofa": s_plt + s_bili + s_cr + s_cv + s_cns, "nee_max": nee}


def logistic_fit(X: np.ndarray, y: np.ndarray, ridge: float = 1.0, iters: int = 50) -> np.ndarray:
    X1 = np.c_[np.ones(len(X)), X]
    b = np.zeros(X1.shape[1])
    pen = np.eye(X1.shape[1]) * ridge
    pen[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X1 @ b))
        W = p * (1 - p)
        g = X1.T @ (y - p) - pen @ b
        Hm = X1.T @ (X1 * W[:, None]) + pen
        step = np.linalg.solve(Hm, g)
        b += step
        if np.abs(step).max() < 1e-8:
            break
    return b


# --------------------------------------------------------------------------- #
# Main per-episode computation
# --------------------------------------------------------------------------- #
def compute(T, ctx, D, tz, log=print, fit_mortality=True):
    enc, mapping, adt, w, runs, ep = (ctx[k] for k in ("enc", "mapping", "adt", "w", "runs", "ep"))
    dm = D["metrics"]
    dval = lambda m, i: next(d["value"] for d in dm[m]["decisions"] if d["id"] == i)

    lo, hi = dval("ltvv", "height_range_cm")
    vt_modes = set(dval("ltvv", "modes"))
    vt_thr = dval("ltvv", "threshold_ml_kg")
    vt_thrs = sorted(set(next((d["value"] for d in dm["ltvv"]["decisions"] if d["id"] == "thresholds_selectable"), []) + [vt_thr]))
    ctx["ltvv_thresholds"] = vt_thrs
    win24 = dval("vt_first24", "window_hours")
    plat_modes = set(dval("plateau", "modes"))
    plat_thr = dval("plateau", "threshold")
    sedatives = dval("sat", "sedatives")
    sbt_elig = dval("sbt", "eligibility")
    ps_max = dval("sbt", "ps_max")
    peep_trial = dval("sbt", "peep_max_trial")
    para_h = dval("paralytic_8h", "hours_threshold")
    reint_h = dval("reintubation", "window_hours")
    rass_lo, rass_hi = dval("light_sedation", "target_range")
    deep = dval("deep_sedation", "definition")
    death_cats = set(dval("mortality", "death"))
    readmit_h = dval("mortality", "readmit_hours")

    vit = T.get("vitals", pd.DataFrame())
    heights = first_height(vit, mapping, lo, hi) if not vit.empty else pd.DataFrame(columns=["height_cm", "height_time"])
    weights = first_weight(vit, mapping) if not vit.empty else pd.Series(dtype=float)
    ctx["heights"], ctx["weights"] = heights, weights

    iv = prep.infusion_intervals(T, mapping, weights, sedatives)
    ctx["iv"] = iv

    pa = T.get("patient_assessments", pd.DataFrame())
    if not pa.empty:
        pa = pa.merge(mapping, on="hospitalization_id")
        pa["cat_l"] = pa["assessment_category"].astype(str).str.lower()
    pa_by = {k: g for k, g in pa.groupby("encounter_block")} if not pa.empty else {}
    labs = T.get("labs", pd.DataFrame())
    if not labs.empty:
        labs = labs.merge(mapping, on="hospitalization_id")
        labs["t"] = labs["lab_collect_dttm"].fillna(labs["lab_result_dttm"])
    labs_by = {k: g for k, g in labs.groupby("encounter_block")} if not labs.empty else {}
    vit_m = vit.merge(mapping, on="hospitalization_id") if not vit.empty else vit
    vit_by = {k: g for k, g in vit_m.groupby("encounter_block")} if not vit_m.empty else {}
    cs = T.get("code_status", pd.DataFrame())
    cs_by = {k: g.sort_values("start_dttm") for k, g in cs.groupby("patient_id")} if not cs.empty else {}
    ctx.update(pa_by=pa_by, labs_by=labs_by, vit_by=vit_by, cs_by=cs_by)

    w_by = {k: g for k, g in w.groupby("encounter_block")}
    runs_by = {k: g for k, g in runs.groupby("encounter_block")}
    enc_i = enc.set_index("encounter_block")
    adt_by = {k: g.sort_values("in_dttm") for k, g in adt.groupby("encounter_block")}

    recs = []
    for e in ep.itertuples():
        blk = e.encounter_block
        E = enc_i.loc[blk]
        g = w_by[blk]
        inep = g[(g["recorded_dttm"] >= e.start) & (g["recorded_dttm"] < e.end) & g["is_imv"]]
        iv_b = iv.get(blk)
        pa_b = pa_by.get(blk)
        h = heights.loc[blk] if blk in heights.index else None
        height = h["height_cm"] if h is not None else np.nan
        p_bw = pbw(height, E["sex_category"])
        r = {"episode_id": e.episode_id, "encounter_block": blk}

        # ---- 1 / 2 LTVV ------------------------------------------------------
        hrs = inep[inep["is_scaffold"] & inep["mode_category"].isin(vt_modes) & inep["tidal_volume_set"].notna()]
        r["vt_hours_total"] = int(len(hrs))
        if not np.isnan(p_bw) and len(hrs):
            ratio = hrs["tidal_volume_set"] / p_bw
            r["ltvv_den"] = int(len(hrs))
            r["ltvv_num"] = int((ratio <= vt_thr + 1e-9).sum())
            for t in vt_thrs:
                r[ltvv_key(t)] = int((ratio <= t + 1e-9).sum())
            first = ratio[hrs["recorded_dttm"] < e.start + pd.Timedelta(hours=win24)]
            r["vt24"] = round(float(first.mean()), 2) if len(first) else None
            r["vt_pbw_median"] = round(float(ratio.median()), 2)
        else:
            r["ltvv_den"] = r["ltvv_num"] = 0
            for t in vt_thrs:
                r[ltvv_key(t)] = 0
            r["vt24"] = None
            r["vt_pbw_median"] = None
        r["pbw"] = round(p_bw, 1) if not np.isnan(p_bw) else None
        r["height_cm"] = float(height) if not pd.isna(height) else None

        # ---- 3 plateau ------------------------------------------------------
        pl = inep[~inep["is_scaffold"] & inep["plateau_pressure_obs"].notna() & inep["mode_category"].isin(plat_modes)]
        r["plat_den"] = int(len(pl))
        r["plat_num"] = int((pl["plateau_pressure_obs"] <= plat_thr).sum())

        # ---- 4 height documented ------------------------------------------
        vt_rows = inep[~inep["is_scaffold"] & inep["tidal_volume_set"].notna()]
        if len(vt_rows):
            r["hdoc_den"] = 1
            t_vt = vt_rows["recorded_dttm"].min()
            r["hdoc_num"] = int(h is not None and h["height_time"] <= t_vt)
        else:
            r["hdoc_den"] = r["hdoc_num"] = 0

        # ---- 8 / 9 SAT, SBT on vent-days -----------------------------------
        sed_on = prep.on_intervals(iv_b, "sedative")
        par_on = prep.on_intervals(iv_b, "paralytic")
        my_runs = runs_by[blk][runs_by[blk]["episode_id"] == e.episode_id]
        sc = inep[inep["is_scaffold"]].copy()
        sc["day"] = sc["recorded_dttm"].dt.tz_convert(tz).dt.date
        sat_den = sat_doc = sat_ph = sat_any = 0
        para_den = para_num = 0
        para_days = []
        icu_segs = _icu_segments(adt_by.get(blk))
        sbt_den = sbt_doc = sbt_ph = sbt_any = 0
        vent_days = 0
        for day, dg in sc.groupby("day"):
            d0 = pd.Timestamp(day, tz=tz).tz_convert("UTC")
            d1 = d0 + pd.Timedelta(days=1)

            # Paralytic metric: denominator = every calendar day with any IMV hour
            # while in an ICU location; numerator = > threshold hours of infusion that day.
            if any(_in_segments(t, icu_segs) for t in dg["recorded_dttm"]):
                para_den += 1
                par_h = _overlap_hours(par_on, d0, d1)
                flag = par_h > para_h + 1e-9
                para_num += int(flag)
                para_days.append((d0, d1, round(par_h, 1), bool(flag)))

            if len(dg) < 6:
                continue
            vent_days += 1
            code = code_status_at(cs_by, E["patient_id"], d1)
            comfort = code.startswith("Comfort")
            on_sed = prep.overlaps(sed_on, d0, d1)
            on_par = prep.overlaps(par_on, d0, d1)
            docs = pa_b[(pa_b["recorded_dttm"] >= d0) & (pa_b["recorded_dttm"] < d1)] if pa_b is not None else None
            # SAT
            if on_sed and not on_par and not comfort:
                sat_den += 1
                dflag = bool(docs is not None and docs["cat_l"].eq("sat_delivery_pass_fail").any())
                pflag = _sat_phenotype(sed_on, my_runs, d0, d1)
                sat_doc += dflag; sat_ph += pflag; sat_any += (dflag or pflag)
            # SBT eligibility at any IMV hour that day
            ok = dg[(dg["fio2_set"] <= sbt_elig["fio2_max"]) & (dg["peep_set"] <= sbt_elig["peep_max"])]
            elig = False
            for t in ok["recorded_dttm"]:
                if not prep.covers(par_on, t) and prep.nee_at(iv_b, t) <= sbt_elig["nee_max"]:
                    elig = True
                    break
            if elig and not comfort:
                sbt_den += 1
                dflag = bool(docs is not None and docs["cat_l"].eq("sbt_delivery_pass_fail").any())
                pflag = _sbt_phenotype(g, my_runs, d0, d1, ps_max, peep_trial)
                sbt_doc += dflag; sbt_ph += pflag; sbt_any += (dflag or pflag)
        r.update(vent_days=vent_days, sat_den=sat_den, sat_doc=sat_doc, sat_pheno=sat_ph, sat_num=sat_any,
                 para8_den=para_den, para8_num=para_num,
                 para8_hours_max=max((p[2] for p in para_days), default=0.0),
                 _para_days=para_days,
                 sbt_den=sbt_den, sbt_doc=sbt_doc, sbt_pheno=sbt_ph, sbt_num=sbt_any)

        # ---- 10 / 11 VFD, duration ------------------------------------------
        died = str(E["discharge_category"]) in death_cats
        tdeath = E["discharge_dttm"] if died else None
        w28 = e.start + pd.Timedelta(days=28)
        all_runs = runs_by[blk]
        vent_d = sum(max(0.0, (min(rr.run_end, w28) - max(rr.run_start, e.start)) / pd.Timedelta(days=1))
                     for rr in all_runs.itertuples() if rr.run_end > e.start and rr.run_start < w28)
        if tdeath is not None and tdeath <= w28:
            r["vfd28"] = 0.0
        else:
            r["vfd28"] = round(max(0.0, 28 - vent_d), 2)
        r["imv_days"] = round(sum((rr.run_end - rr.run_start) / pd.Timedelta(days=1) for rr in my_runs.itertuples()), 2)

        # ---- 12 reintubation -------------------------------------------------
        ext_den = reint = 0
        for rr in my_runs.itertuples():
            if not rr.ended_by_device_change or rr.next_device == "trach collar":
                continue  # still on IMV at end of data, or liberated to trach collar
            if tdeath is not None and tdeath <= rr.run_end + pd.Timedelta(hours=24):
                continue  # death within 24 h
            if limited_code_between(cs_by, E["patient_id"], rr.run_end - pd.Timedelta(hours=24), rr.run_end):
                continue  # comfort-care extubation
            ext_den += 1
            later = all_runs[(all_runs["run_start"] > rr.run_end) &
                             (all_runs["run_start"] <= rr.run_end + pd.Timedelta(hours=reint_h))]
            reint += int(not later.empty)
        r["ext_den"], r["reint_num"] = ext_den, reint

        # ---- 13 / 14 sedation -----------------------------------------------
        if pa_b is not None:
            rass = pa_b[pa_b["cat_l"].eq("rass") & pa_b["numerical_value"].notna()]
            # boolean masks must be Series: df[[]] (empty list) selects zero *columns*, not zero rows
            rass = rass[pd.Series([any(rr.run_start <= t < rr.run_end for rr in my_runs.itertuples()) for t in rass["recorded_dttm"]], index=rass.index, dtype=bool)]
            rass_np = rass[pd.Series([not prep.covers(par_on, t) for t in rass["recorded_dttm"]], index=rass.index, dtype=bool)]
            r["rass_den"] = int(len(rass_np))
            r["rass_num"] = int(rass_np["numerical_value"].between(rass_lo, rass_hi).sum())
            early = rass[rass["recorded_dttm"] < e.start + pd.Timedelta(hours=deep["window_hours"])]
            if len(early):
                r["deep_den"] = 1
                r["deep_num"] = int((early["numerical_value"] <= deep["threshold"]).mean() > deep["share"])
            else:
                r["deep_den"] = r["deep_num"] = 0
            r["rass_median"] = float(rass["numerical_value"].median()) if len(rass) else None
        else:
            r.update(rass_den=0, rass_num=0, deep_den=0, deep_num=0, rass_median=None)

        # ---- risk factors at start ------------------------------------------
        sw = sofa_window(labs_by.get(blk), vit_by.get(blk), pa_b, iv_b, e.start - pd.Timedelta(hours=24), e.start)
        r["sofa"] = sw["sofa"]
        r["code_status"] = code_status_at(cs_by, E["patient_id"], e.start)
        recs.append(r)

    # per-day paralytic detail is for the timeline only; keep it out of the episode table
    ctx["para_days"] = {r["episode_id"]: r.pop("_para_days") for r in recs}
    m = pd.DataFrame(recs)
    ep = ep.merge(m, on=["episode_id", "encounter_block"], how="left")

    # ---- 15 outcomes (per encounter, on its first included episode) ----------
    ep = ep.sort_values(["encounter_block", "start"])
    ep["first_in_enc"] = ~ep.duplicated("encounter_block")
    outc = []
    for blk in ep["encounter_block"].unique():
        E = enc_i.loc[blk]
        a = adt_by.get(blk, pd.DataFrame())
        icu_days, readmit_den, readmit = 0.0, 0, 0
        if not a.empty:
            icu_flag = a["location_category"].str.lower().eq("icu").to_numpy()
            ins, outs = a["in_dttm"].to_numpy(), a["out_dttm"].to_numpy()
            for i in range(len(a)):
                if icu_flag[i] and not pd.isna(outs[i]):
                    icu_days += (pd.Timestamp(outs[i]) - pd.Timestamp(ins[i])) / pd.Timedelta(days=1)
            # transfers out of ICU to a non-ICU location (alive, before discharge)
            for i in range(len(a) - 1):
                if icu_flag[i] and not icu_flag[i + 1]:
                    readmit_den = 1
                    t_out = pd.Timestamp(outs[i]) if not pd.isna(outs[i]) else pd.Timestamp(ins[i + 1])
                    back = [j for j in range(i + 1, len(a)) if icu_flag[j] and
                            pd.Timestamp(ins[j]) <= t_out + pd.Timedelta(hours=readmit_h)]
                    if back:
                        readmit = 1
                    break
        g = w_by.get(blk)
        trach = int(g is not None and "tracheostomy" in g and (g["tracheostomy"] == 1).any())
        outc.append({"encounter_block": blk, "died": int(str(E["discharge_category"]) in death_cats),
                     "icu_los": round(icu_days, 2), "trach_any": trach,
                     "readmit_den": readmit_den, "readmit_num": readmit,
                     "hosp_los": round((E["discharge_dttm"] - E["admission_dttm"]) / pd.Timedelta(days=1), 2)})
    oc = pd.DataFrame(outc)
    ep = ep.merge(oc, on="encounter_block", how="left")
    for c in ("died", "trach_any", "readmit_den", "readmit_num"):
        ep[c] = np.where(ep["first_in_enc"], ep[c], 0)
    ep["outcome_den"] = ep["first_in_enc"].astype(int)
    ep.loc[~ep["first_in_enc"], ["icu_los"]] = np.nan

    ep = add_cci(ep, ctx, enc)
    if fit_mortality:
        ep = fit_mortality_model(ep, ctx)
    return ep


def add_cci(ep, ctx, enc):
    """clif_dashboards: split out of compute() so chunks can be processed independently."""
    mapping = ctx["mapping"]
    cci = ctx.get("cci")
    if cci is not None and not cci.empty:
        cmap = cci.merge(mapping, on="hospitalization_id").groupby("encounter_block")["cci_score"].max()
        ep["cci"] = ep["encounter_block"].map(cmap).fillna(0)
    else:
        ep["cci"] = 0
    ep = ep.merge(enc[["encounter_block", "age", "sex_category"]], on="encounter_block", how="left")
    return ep


def fit_mortality_model(ep, ctx):
    """Site-fitted expected mortality over all included episodes (needs the whole cohort)."""
    ep = ep.reset_index(drop=True)
    fit = ep[ep["first_in_enc"]]
    X = np.c_[(fit["age"] - 60) / 10, fit["sex_category"].astype(str).str.lower().str.startswith("f"),
              fit["sofa"].fillna(0), fit["cci"]].astype(float)
    y = fit["died"].to_numpy(float)
    ep["mort_expected"] = np.nan
    model = None
    if len(fit) >= 10 and 0 < y.sum() < len(y):
        b = logistic_fit(X, y)
        p = 1 / (1 + np.exp(-(np.c_[np.ones(len(X)), X] @ b)))
        ep.loc[fit.index, "mort_expected"] = p
        model = {"terms": ["intercept", "age per 10 y (centred 60)", "female", "SOFA", "Charlson"],
                 "coef": [round(float(x), 4) for x in b], "n": int(len(fit)), "events": int(y.sum())}
    ctx["mort_model"] = model
    return ep


def _sat_phenotype(sed_on, my_runs, d0, d1) -> bool:
    """All sedatives off ≥ 30 min while on IMV, after having been on, starting this day."""
    if not sed_on:
        return False
    for i, (a, b) in enumerate(sed_on):
        off_start = b
        off_end = sed_on[i + 1][0] if i + 1 < len(sed_on) else None
        if not (d0 <= off_start < d1):
            continue
        for rr in my_runs.itertuples():
            if rr.run_start <= off_start < rr.run_end:
                end = min(x for x in [off_end, rr.run_end] if x is not None)
                if end - off_start >= pd.Timedelta(minutes=30):
                    return True
    return False


def _icu_segments(adt_b) -> list[tuple]:
    """[(in, out)] ADT stays with location_category = icu (open stays run to +inf)."""
    if adt_b is None or adt_b.empty:
        return []
    icu = adt_b[adt_b["location_category"].astype(str).str.lower().eq("icu")]
    far = pd.Timestamp("2262-01-01", tz="UTC")
    return [(a, b if not pd.isna(b) else far) for a, b in zip(icu["in_dttm"], icu["out_dttm"])]


def _in_segments(t, segs) -> bool:
    # scaffold rows sit at HH:59:59 and stand for that hour: test the hour's midpoint
    mid = t - pd.Timedelta(minutes=30)
    return any(a <= mid < b for a, b in segs)


def _overlap_hours(intervals, d0, d1) -> float:
    total = pd.Timedelta(0)
    for a, b in intervals:
        s = max(a, d0)
        e = min(b, d1)
        if s < e:
            total += (e - s)
    return float(total / pd.Timedelta(hours=1))


def _sbt_phenotype(g, my_runs, d0, d1, ps_max, peep_max) -> bool:
    real = g[~g["is_scaffold"] & (g["recorded_dttm"] >= d0 - pd.Timedelta(hours=6)) & (g["recorded_dttm"] < d1)]
    if real.empty:
        return False
    mode = real["mode_category"].astype(str)
    q = ((mode.eq("pressure support/cpap") & (real["pressure_support_set"].fillna(99) <= ps_max)
          & (real["peep_set"].fillna(99) <= peep_max)) | mode.eq("blow by")) & real["is_imv"]
    t = real["recorded_dttm"].to_numpy()
    qa = q.to_numpy()
    i = 0
    while i < len(qa):
        if qa[i]:
            j = i
            while j + 1 < len(qa) and qa[j + 1]:
                j += 1
            start = pd.Timestamp(t[i])
            stop = pd.Timestamp(t[j + 1]) if j + 1 < len(qa) else pd.Timestamp(t[j])
            if d0 <= start < d1 and stop - start >= pd.Timedelta(minutes=30):
                return True
            i = j + 1
        else:
            i += 1
    return False
