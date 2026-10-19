"""Build one episode's hourly timeline from the staged chunk that holds it.

Upstream precomputed timelines for up to 3000 episodes and embedded them in the
HTML (95% of its size). Here the server reads only the patient's rows from the
staged parquet, rebuilds the same context upstream had (outliers removed,
infusion intervals, assessments keyed by encounter), and calls upstream's
timelines() for that single episode.
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd

from ... import stage
from ...upstream import clif_io, metrics, prep
from ...upstream.timelines import timelines
from ...server.store import Store, jsonable

TIMELINE_TABLES = ["vitals", "labs", "position", "patient_assessments", "medication_admin_continuous"]


class TimelineBuilder:
    def __init__(self, store: Store, cache_size: int = 256):
        self.store = store
        cfg = store.cfg
        work = cfg.stage_dir
        self.paralytic_days = pd.read_parquet(cfg.dashboard_dir / "paralytic_days.parquet")
        self.chunk_of_block = pd.read_parquet(work / "chunks_b.parquet").set_index("encounter_block")["chunk"]
        self.mapping = pd.read_parquet(work / "mapping.parquet")
        self.fio2_percent = bool(store.meta.get("fio2_recorded_as_percent", False))
        self.sedatives = next(d["value"] for d in store.decisions["metrics"]["sat"]["decisions"] if d["id"] == "sedatives")
        self.build = lru_cache(maxsize=cache_size)(self._build)

    def _build(self, episode_id: str) -> dict | None:
        episodes = self.store.episodes
        match = episodes[episodes["episode_id"] == episode_id]
        if match.empty:
            return None
        episode = match.iloc[[0]]
        block = episode["encounter_block"].iloc[0]
        chunk = int(self.chunk_of_block[block])
        cfg = self.store.cfg
        mapping = self.mapping[self.mapping["encounter_block"] == block].reset_index(drop=True)
        hospitalization_ids = mapping["hospitalization_id"].tolist()

        tables = {t: stage.load_staged_chunk(cfg, "phase_b", t, chunk, hospitalization_ids) for t in TIMELINE_TABLES}
        clif_io.apply_outliers(tables, log=lambda m: None, fio2_percent=self.fio2_percent)
        weights = metrics.first_weight(tables["vitals"], mapping) if not tables["vitals"].empty else pd.Series(dtype=float)
        infusions = prep.infusion_intervals(tables, mapping, weights, self.sedatives)
        assessments = tables["patient_assessments"]
        assessments_by_block = {}
        if not assessments.empty:
            assessments = assessments.merge(mapping, on="hospitalization_id")
            assessments["cat_l"] = assessments["assessment_category"].astype(str).str.lower()
            assessments_by_block = {k: g for k, g in assessments.groupby("encounter_block")}

        waterfall = pd.read_parquet(cfg.stage_dir / "waterfall" / f"chunk_{chunk}.parquet",
                                    filters=[("encounter_block", "==", block)])
        waterfall = waterfall.drop(columns=["waterfall_row", "phase_a_chunk", "chunk"]).reset_index(drop=True)
        days = self.paralytic_days
        days = days[days["episode_id"] == episode_id]
        context = {
            "w": waterfall, "T": tables, "mapping": mapping, "pa_by": assessments_by_block, "iv": infusions,
            "para_days": {episode_id: list(zip(days["day_start"], days["day_end"],
                                               days["paralytic_hours"], days["over_threshold"]))},
        }
        series = timelines(context, episode, None, cfg.site_timezone, 1)[episode_id]
        return jsonable(series)
