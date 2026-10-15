"""Shared preparation: cohort, encounter blocks, waterfall, IMV runs/episodes,
unit attribution, strata, medication timelines."""
from __future__ import annotations

import numpy as np
import pandas as pd

H = pd.Timedelta(hours=1)

SEDATIVES_DEFAULT = ["propofol", "midazolam", "lorazepam", "dexmedetomidine", "ketamine"]
PARALYTIC_GROUP = "paralytics"
# Also match by drug, in case a site maps a paralytic's med_group differently
PARALYTIC_CATS = ["cisatracurium", "rocuronium", "vecuronium", "atracurium", "pancuronium", "succinylcholine"]
VASO = ["norepinephrine", "epinephrine", "phenylephrine", "vasopressin", "dopamine", "angiotensin"]

UNIT_LABELS = {
    "medical_icu": "Medical ICU", "surgical_icu": "Surgical ICU", "general_icu": "General ICU",
    "cvicu_icu": "Cardiovascular ICU", "cardiac_icu": "Cardiac ICU", "mixed_neuro_icu": "Neuro ICU",
    "neuro_icu": "Neuro ICU", "mixed_cardiothoracic_icu": "Cardiothoracic ICU", "burn_icu": "Burn ICU",
    "trauma_icu": "Trauma ICU", "mixed_icu": "Mixed ICU",
}


def unit_label(x) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)) or str(x) in ("", "None", "nan"):
        return "ICU (type not mapped)"
    return UNIT_LABELS.get(str(x), str(x).replace("_", " ").title())


# --------------------------------------------------------------------------- #
# Demo: re-anchor MIMIC dates
# --------------------------------------------------------------------------- #
def reanchor_dates(T: dict, start: str, end: str, log=print) -> None:
    """Shift each patient's datetimes by whole weeks into [start, end].

    MIMIC-IV shifts dates per patient into 2110-2201. To make monthly trends
    readable in the demo we spread patients across the window in the order of
    their first admission. Whole-week shifts keep weekday and time of day.
    """
    h = T["hospitalization"]
    # MIMIC patients can have admissions decades apart, so shift per hospitalization,
    # keeping a patient's admissions in order and < 6 h gaps intact (for stitching).
    hh = h.sort_values("admission_dttm")[["patient_id", "hospitalization_id", "admission_dttm", "discharge_dttm"]]
    w0 = pd.Timestamp(start, tz="UTC")
    w1 = pd.Timestamp(end, tz="UTC") - pd.Timedelta(days=60)
    n = len(hh)
    targets = [w0 + (w1 - w0) * (i / max(n - 1, 1)) for i in range(n)]
    shifts = {}
    prev = {}
    for (pid, hid, adm, dis), tgt in zip(hh.itertuples(index=False), targets):
        p = prev.get(pid)
        if p is not None and adm - p[1] < pd.Timedelta(hours=24):
            sh = p[2]  # contiguous stay: same shift as the previous hospitalization
        else:
            if p is not None:  # keep separate stays ≥ 30 days apart after shifting
                tgt = max(tgt, p[1] + p[2] + pd.Timedelta(days=30))
            sh = pd.Timedelta(days=round((tgt - adm) / pd.Timedelta(days=1)))
        shifts[hid] = sh
        prev[pid] = (adm, dis, sh)
    hsh = pd.Series(shifts)
    # patient-keyed rows: use the hospitalization they fall in (else the nearest earlier one)
    spans = hh.assign(sh=hh["hospitalization_id"].map(hsh))

    def patient_shift(pids, times):
        out = []
        for pid, t in zip(pids, times):
            g = spans[spans["patient_id"] == pid]
            if g.empty:
                out.append(pd.Timedelta(0))
                continue
            if pd.isna(t):
                out.append(g["sh"].iloc[-1])
                continue
            before = g[g["admission_dttm"] <= t + pd.Timedelta(hours=24)]
            out.append((before if len(before) else g)["sh"].iloc[-1 if len(before) else 0])
        return pd.Series(out, index=pids.index)

    for name, df in T.items():
        if df is None or df.empty:
            continue
        dcols = [c for c in df.columns if c.endswith("_dttm")]
        if not dcols:
            continue
        if "hospitalization_id" in df.columns:
            s = df["hospitalization_id"].map(hsh).fillna(pd.Timedelta(0))
        elif "patient_id" in df.columns:
            s = patient_shift(df["patient_id"], df[dcols[0]])
        else:
            continue
        for c in dcols:
            df[c] = df[c] + s
    log(f"  - demo: re-anchored {n} hospitalizations' dates into {start} … {end}")


# --------------------------------------------------------------------------- #
# Encounters
# --------------------------------------------------------------------------- #
def build_encounters(T, stitch_fn, stitch_hours, age_min, log=print):
    hosp = T["hospitalization"].copy()
    adt = T["adt"].copy()
    hs, adt_s, mapping = stitch_fn(hosp, adt, time_interval=stitch_hours)
    mapping = mapping[["hospitalization_id", "encounter_block"]].drop_duplicates()
    hosp = hosp.merge(mapping, on="hospitalization_id", how="left")
    hosp = hosp.sort_values("admission_dttm")
    enc = hosp.groupby("encounter_block").agg(
        patient_id=("patient_id", "first"),
        hospitalization_id=("hospitalization_id", "first"),
        admission_dttm=("admission_dttm", "min"),
        discharge_dttm=("discharge_dttm", "max"),
        age=("age_at_admission", "first"),
        discharge_category=("discharge_category", "last"),
        n_hosp=("hospitalization_id", "nunique"),
    ).reset_index()
    pat = T["patient"]
    enc = enc.merge(pat[["patient_id", "sex_category", "race_category", "ethnicity_category"]],
                    on="patient_id", how="left")
    enc["adult"] = enc["age"] >= age_min
    adt = adt.merge(mapping, on="hospitalization_id", how="left")
    log(f"  - encounters: {len(enc):,} ({hosp.hospitalization_id.nunique():,} hospitalizations stitched)")
    return enc, mapping, adt


def race_eth(race, eth) -> str:
    r = str(race).lower()
    e = str(eth).lower()
    if e == "hispanic":
        return "Hispanic"
    if r == "white":
        return "White, non-Hispanic"
    if r.startswith("black"):
        return "Black, non-Hispanic"
    if r == "asian":
        return "Asian, non-Hispanic"
    if r in ("unknown", "none", "nan", ""):
        return "Unknown"
    return "Other"


def age_band(a) -> str:
    if pd.isna(a):
        return "Unknown"
    a = float(a)
    return "18–44" if a < 45 else "45–64" if a < 65 else "65–79" if a < 80 else "80+"


# --------------------------------------------------------------------------- #
# Ventilation runs and episodes
# --------------------------------------------------------------------------- #
def waterfall(T, mapping, waterfall_fn):
    rs = T["respiratory_support"].copy()
    rs = rs.merge(mapping, on="hospitalization_id", how="inner")
    if "tracheostomy" in rs.columns:
        rs["tracheostomy"] = pd.to_numeric(rs["tracheostomy"].replace({True: 1, False: 0, "True": 1, "False": 0}),
                                           errors="coerce")
    rs = rs.drop(columns=["hospitalization_id"])
    w = waterfall_fn(rs, id_col="encounter_block", verbose=False)
    w = w.sort_values(["encounter_block", "recorded_dttm"]).reset_index(drop=True)
    w["is_scaffold"] = w["is_scaffold"].fillna(False).astype(bool)
    w["is_imv"] = w["device_category"].eq("imv")
    return w


def imv_runs(w: pd.DataFrame) -> pd.DataFrame:
    """Contiguous IMV stretches per encounter (start, end, next device)."""
    out = []
    for blk, g in w.groupby("encounter_block", sort=False):
        imv = g["is_imv"].to_numpy()
        if not imv.any():
            continue
        t = g["recorded_dttm"].to_numpy()
        dev = g["device_category"].to_numpy()
        trach = g["tracheostomy"].to_numpy() if "tracheostomy" in g else np.zeros(len(g))
        change = np.diff(np.r_[0, imv.astype(int), 0])
        starts = np.where(change == 1)[0]
        ends = np.where(change == -1)[0]  # index of first non-imv row after run
        for s, e in zip(starts, ends):
            nxt = e if e < len(g) else None
            out.append({
                "encounter_block": blk,
                "run_start": pd.Timestamp(t[s]),
                "run_end": pd.Timestamp(t[nxt]) if nxt is not None else pd.Timestamp(t[e - 1]),
                "next_device": dev[nxt] if nxt is not None else None,
                "ended_by_device_change": nxt is not None,
                "trach_during": bool(np.nansum(trach[s:e]) > 0),
            })
    r = pd.DataFrame(out)
    if r.empty:
        return pd.DataFrame(columns=["encounter_block", "run_start", "run_end", "next_device",
                                     "ended_by_device_change", "trach_during"])
    r["run_start"] = pd.to_datetime(r["run_start"], utc=True)
    r["run_end"] = pd.to_datetime(r["run_end"], utc=True)
    return r


def episodes_from_runs(runs: pd.DataFrame, gap_hours: float) -> pd.DataFrame:
    runs = runs.sort_values(["encounter_block", "run_start"]).copy()
    prev_end = runs.groupby("encounter_block")["run_end"].shift()
    new_ep = prev_end.isna() | ((runs["run_start"] - prev_end) >= pd.Timedelta(hours=gap_hours))
    runs["episode_seq"] = new_ep.groupby(runs["encounter_block"]).cumsum().astype(int)
    ep = runs.groupby(["encounter_block", "episode_seq"]).agg(
        start=("run_start", "min"), end=("run_end", "max"), n_runs=("run_start", "size"),
        trach=("trach_during", "max"),
    ).reset_index()
    ep["episode_id"] = ep["encounter_block"].astype(str) + "-" + ep["episode_seq"].astype(str)
    runs["episode_id"] = runs["encounter_block"].astype(str) + "-" + runs["episode_seq"].astype(str)
    return ep, runs


def attribute_unit(ep: pd.DataFrame, adt: pd.DataFrame) -> pd.DataFrame:
    adt = adt.sort_values(["encounter_block", "in_dttm"])
    icu = adt[adt["location_category"].str.lower().eq("icu")]
    by_blk = {k: g for k, g in icu.groupby("encounter_block")}
    units, hosps, overlap = [], [], []
    for r in ep.itertuples():
        g = by_blk.get(r.encounter_block)
        if g is None:
            units.append(None); hosps.append(None); overlap.append(False)
            continue
        ov = g[(g["in_dttm"] <= r.end) & (g["out_dttm"].fillna(r.end) >= r.start)]
        if ov.empty:
            units.append(None); hosps.append(None); overlap.append(False)
            continue
        at = ov[(ov["in_dttm"] <= r.start) & (ov["out_dttm"].fillna(r.end) >= r.start)]
        row = at.iloc[0] if not at.empty else ov.iloc[0]
        units.append(unit_label(row.get("location_type")))
        hosps.append(str(row.get("hospital_id")))
        overlap.append(True)
    ep = ep.copy()
    ep["unit"] = units
    ep["hospital"] = hosps
    ep["icu_overlap"] = overlap
    return ep


# --------------------------------------------------------------------------- #
# Medication timelines (continuous infusions)
# --------------------------------------------------------------------------- #
def _to_mcg_kg_min(dose, unit, wt):
    u = str(unit).lower().replace(" ", "")
    if dose is None or pd.isna(dose):
        return np.nan
    if u in ("mcg/kg/min",):
        return dose
    if u in ("mcg/kg/hr", "mcg/kg/hour"):
        return dose / 60
    if u in ("mcg/min",):
        return dose / wt
    if u in ("mcg/hr", "mcg/hour"):
        return dose / wt / 60
    if u in ("mg/min",):
        return dose * 1000 / wt
    if u in ("mg/hr", "mg/hour"):
        return dose * 1000 / wt / 60
    if u in ("ng/kg/min",):
        return dose / 1000
    return np.nan


def _vaso_units_min(dose, unit):
    u = str(unit).lower().replace(" ", "")
    if u in ("units/min", "unit/min", "u/min"):
        return dose
    if u in ("units/hr", "units/hour", "u/hr"):
        return dose / 60
    return np.nan


def infusion_intervals(T, mapping, weights: pd.Series, sedatives: list[str]) -> dict:
    """Return per-encounter interval tables for sedatives, paralytics, and NEE."""
    mc = T.get("medication_admin_continuous")
    if mc is None or mc.empty:
        return {}
    mc = mc.merge(mapping, on="hospitalization_id", how="inner").copy()
    mc["med_category"] = mc["med_category"].astype(str).str.lower()
    mc["med_group"] = mc["med_group"].astype(str).str.lower()
    act = mc.get("mar_action_category", pd.Series("", index=mc.index)).astype(str).str.lower()
    grp = mc.get("mar_action_group", pd.Series("", index=mc.index)).astype(str).str.lower()
    stopped = act.isin(["stop", "stopped", "paused", "hold", "held"]) | grp.eq("not_administered")
    mc["dose"] = pd.to_numeric(mc["med_dose"], errors="coerce").where(~stopped, 0.0).fillna(0.0)
    is_par = mc["med_group"].eq(PARALYTIC_GROUP) | mc["med_category"].isin(PARALYTIC_CATS)
    keep = mc["med_category"].isin(sedatives) | is_par | mc["med_category"].isin(VASO)
    is_par = is_par[keep]
    mc = mc[keep].sort_values(["encounter_block", "med_category", "admin_dttm"])
    # Interval until the next event for the same drug; last event lasts up to 6 h
    nxt = mc.groupby(["encounter_block", "med_category"])["admin_dttm"].shift(-1)
    mc["until"] = nxt.fillna(mc["admin_dttm"] + pd.Timedelta(hours=6))
    mc["kind"] = np.where(is_par.reindex(mc.index).to_numpy(), "paralytic",
                          np.where(mc["med_category"].isin(sedatives), "sedative", "vaso"))
    wt = mc["encounter_block"].map(weights).fillna(80.0)
    nee = np.full(len(mc), np.nan)
    cats = mc["med_category"].to_numpy()
    doses = mc["dose"].to_numpy(dtype=float)
    units = mc["med_dose_unit"].to_numpy()
    wts = wt.to_numpy()
    for i in range(len(mc)):
        c = cats[i]
        if c in ("norepinephrine", "epinephrine"):
            nee[i] = _to_mcg_kg_min(doses[i], units[i], wts[i])
        elif c == "phenylephrine":
            nee[i] = _to_mcg_kg_min(doses[i], units[i], wts[i]) * 0.06 if doses[i] else 0
        elif c == "dopamine":
            nee[i] = _to_mcg_kg_min(doses[i], units[i], wts[i]) / 100 if doses[i] else 0
        elif c == "vasopressin":
            nee[i] = _vaso_units_min(doses[i], units[i]) * 2.5 if doses[i] else 0
        elif c == "angiotensin":
            nee[i] = (doses[i] * 0.0025) if doses[i] else 0  # ng/kg/min per study
    mc["nee"] = nee
    out = {}
    for blk, g in mc.groupby("encounter_block"):
        out[blk] = g[["admin_dttm", "until", "med_category", "kind", "dose", "nee"]].reset_index(drop=True)
    return out


def on_intervals(iv: pd.DataFrame | None, kind: str) -> list[tuple]:
    """Merged [start, end) intervals where any drug of `kind` is running (dose > 0)."""
    if iv is None or iv.empty:
        return []
    g = iv[(iv["kind"] == kind) & (iv["dose"] > 0)].sort_values("admin_dttm")
    merged = []
    for s, e in zip(g["admin_dttm"], g["until"]):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def overlaps(intervals, s, e) -> bool:
    return any(a < e and b > s for a, b in intervals)


def covers(intervals, t) -> bool:
    return any(a <= t < b for a, b in intervals)


def nee_at(iv: pd.DataFrame | None, t) -> float:
    if iv is None or iv.empty:
        return 0.0
    g = iv[(iv["kind"] == "vaso") & (iv["admin_dttm"] <= t) & (iv["until"] > t)]
    return float(g["nee"].fillna(0).sum()) if not g.empty else 0.0


def nee_max(iv: pd.DataFrame | None, s, e) -> float:
    if iv is None or iv.empty:
        return 0.0
    g = iv[(iv["kind"] == "vaso") & (iv["admin_dttm"] < e) & (iv["until"] > s)]
    if g.empty:
        return 0.0
    # evaluate total at each event time inside the window
    pts = sorted(set([s] + [t for t in g["admin_dttm"] if s <= t < e]))
    return max(nee_at(g, t) for t in pts)
