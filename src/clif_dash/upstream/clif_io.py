"""Load CLIF tables and the clifpy functions the dashboard relies on.

clifpy is used directly when it is installed (`pip install clifpy`). When it
is not, the same clifpy source files vendored under pipeline/vendor/clifpy
(Apache-2.0, commit 39cabd6) are loaded instead, so results are identical.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

VENDOR = Path(__file__).parent / "vendor" / "clifpy"

TABLES = [
    "patient", "hospitalization", "adt", "respiratory_support", "vitals", "labs",
    "position", "patient_assessments", "medication_admin_continuous",
    "code_status", "hospital_diagnosis",
]
REQUIRED = {"patient", "hospitalization", "adt", "respiratory_support"}


# --------------------------------------------------------------------------- #
# Reading tables
# --------------------------------------------------------------------------- #
def _read_parquet(path: Path) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except ImportError:
        from .pq_fallback import read_parquet
        return read_parquet(str(path))


def load_tables(clif_dir: Path, filetype: str, prefix: str, log=print) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for t in TABLES:
        f = clif_dir / f"{prefix}{t}.{filetype}"
        if not f.exists():
            if t in REQUIRED:
                raise FileNotFoundError(f"Required CLIF table not found: {f}")
            log(f"  - {t}: not found (metrics needing it will show as unavailable)")
            out[t] = pd.DataFrame()
            continue
        df = _read_parquet(f) if filetype == "parquet" else pd.read_csv(f, low_memory=False)
        # CLIF datetimes are UTC
        for c in df.columns:
            if c.endswith("_dttm"):
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
        for c in ("hospitalization_id", "patient_id"):
            if c in df.columns:
                df[c] = df[c].astype(str)
        out[t] = df
        log(f"  - {t}: {len(df):,} rows")
    return out


# --------------------------------------------------------------------------- #
# clifpy functions (installed package, else vendored copy)
# --------------------------------------------------------------------------- #
def _shim_optional_modules():
    """Waterfall imports duckdb/tqdm; both are optional for it (pandas fallback)."""
    try:
        import duckdb  # noqa: F401
    except ImportError:
        m = types.ModuleType("duckdb")
        def connect(*a, **k):
            raise ImportError("duckdb not installed")
        m.connect = connect
        sys.modules["duckdb"] = m
    try:
        import tqdm  # noqa: F401
    except ImportError:
        m = types.ModuleType("tqdm")
        class _T:
            def __init__(self, *a, **k):
                pass
            @staticmethod
            def pandas(*a, **k):
                pd.DataFrame.progress_apply = pd.DataFrame.apply
                from pandas.core.groupby import DataFrameGroupBy, SeriesGroupBy
                DataFrameGroupBy.progress_apply = DataFrameGroupBy.apply
                SeriesGroupBy.progress_apply = SeriesGroupBy.apply
                pd.Series.progress_apply = pd.Series.apply
        m.tqdm = _T
        sys.modules["tqdm"] = m


def _load_vendored(name: str):
    _shim_optional_modules()
    spec = importlib.util.spec_from_file_location(f"_vendored_clifpy_{name}", VENDOR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clifpy_functions():
    """Return (waterfall_fn, stitch_fn, source_label)."""
    try:
        from clifpy.utils.waterfall import process_resp_support_waterfall
        from clifpy.utils.stitching_encounters import stitch_encounters
        import clifpy
        return process_resp_support_waterfall, stitch_encounters, f"clifpy {getattr(clifpy, '__version__', '')}".strip()
    except Exception:
        wf = _load_vendored("waterfall").process_resp_support_waterfall
        st = _load_vendored("stitching_encounters").stitch_encounters
        return wf, st, "clifpy (vendored source, commit 39cabd6)"


def outlier_config() -> dict:
    try:
        import clifpy
        p = Path(clifpy.__file__).parent / "schemas" / "outlier_config.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text())["tables"]
    except Exception:
        pass
    return yaml.safe_load((VENDOR / "outlier_config.yaml").read_text())["tables"]


def apply_outliers(tables: dict[str, pd.DataFrame], log=print, fio2_percent: bool | None = None) -> dict:
    """Set out-of-range values to NaN using clifpy's outlier thresholds.

    clif_dashboards: `fio2_percent` carries the whole-table "FiO2 recorded as percent" test
    (mean > 1) so that a chunk of the table is rescaled exactly as the full table would be.
    """
    cfg = outlier_config()
    summary = {}
    # wide tables: per column
    for t in ("respiratory_support",):
        df = tables.get(t)
        if df is None or df.empty or t not in cfg:
            continue
        for col, rng in cfg[t].items():
            if col in df.columns and isinstance(rng, dict) and "min" in rng:
                v = pd.to_numeric(df[col], errors="coerce")
                if col == "fio2_set" and (v.mean(skipna=True) > 1 if fio2_percent is None else fio2_percent):
                    v = v.where(v <= 1, v / 100)  # 40 -> 0.40 before range check
                bad = v.notna() & ((v < rng["min"]) | (v > rng["max"]))
                summary[f"{t}.{col}"] = int(bad.sum())
                df[col] = v.mask(bad)
    # long tables: per category
    for t, valcol, catcol in (("vitals", "vital_value", "vital_category"),
                              ("labs", "lab_value_numeric", "lab_category"),
                              ("patient_assessments", "numerical_value", "assessment_category")):
        df = tables.get(t)
        if df is None or df.empty or t not in cfg:
            continue
        rules = cfg[t].get(valcol, {})
        if valcol not in df.columns:
            if t == "labs" and "lab_value" in df.columns:
                df["lab_value_numeric"] = pd.to_numeric(df["lab_value"], errors="coerce")
            else:
                continue
        v = pd.to_numeric(df[valcol], errors="coerce")
        lo = df[catcol].map({k: r.get("min") for k, r in rules.items() if isinstance(r, dict)})
        hi = df[catcol].map({k: r.get("max") for k, r in rules.items() if isinstance(r, dict)})
        bad = v.notna() & (((lo.notna()) & (v < lo)) | ((hi.notna()) & (v > hi)))
        summary[t] = int(bad.sum())
        df[valcol] = v.mask(bad)
    log(f"  - outliers set to missing: {sum(summary.values()):,}")
    return summary


def charlson(hospital_diagnosis: pd.DataFrame, log=lambda message: None) -> pd.DataFrame:
    """Charlson index (Quan 2011), same logic and YAML as clifpy.calculate_cci.

    clif_dashboards: `log` reports each step and why the vendored fallback ran, which
    the original swallowed silently.
    """
    try:
        from clifpy.utils.comorbidity import calculate_cci
        log("clifpy calculate_cci: started")
        result = calculate_cci(hospital_diagnosis)[["hospitalization_id", "cci_score"]]
        log(f"clifpy calculate_cci: finished, {len(result):,} hospitalizations scored")
        return result
    except Exception as error:
        log(f"clifpy calculate_cci failed ({type(error).__name__}: {error}); using the vendored implementation")
    if hospital_diagnosis is None or hospital_diagnosis.empty:
        return pd.DataFrame(columns=["hospitalization_id", "cci_score"])
    cfg = yaml.safe_load((VENDOR / "cci.yaml").read_text())
    df = hospital_diagnosis[hospital_diagnosis["diagnosis_code_format"].str.lower() == "icd10cm"].copy()
    df["code"] = df["diagnosis_code"].astype(str).str.lower().str.replace(".", "", regex=False)
    conds = cfg["diagnosis_code_mappings"]["ICD10CM"]
    for name, info in conds.items():
        prefixes = tuple(c.lower() for c in info["codes"])
        df[name] = df["code"].str.startswith(prefixes)
    g = df.groupby("hospitalization_id")[list(conds)].max().astype(int)
    for _, lst in cfg["hierarchies"].items():
        if len(lst) >= 2:
            for mild in lst[1:]:
                g.loc[g[lst[0]] == 1, mild] = 0
    g["cci_score"] = sum(g[c] * w for c, w in cfg["weights"].items() if c in g)
    log(f"vendored Charlson index: finished, {len(g):,} hospitalizations scored")
    return g.reset_index()[["hospitalization_id", "cci_score"]]
