# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

This project serves ICU quality-improvement dashboards for UCSF from the site's CLIF 2.1 database (80K+ ICU admissions). It derives from the collaborator draft at https://github.com/cag-lab/clif-icu-quality-dashboard ("upstream", commit `b3cdf29`), which computes respiratory-failure quality measures and writes one self-contained HTML file. That design does not scale to UCSF, so this repo splits it into a batch build and a server that computes report pieces on request.

Settled decisions: Python throughout; it runs on the maintainer's laptop for now, with UCSF hosting possible later; aggregation runs on the server, never in the browser. The license is undecided (the maintainer has asked the upstream author about relicensing); until then the project stays internal, and code derived from upstream is GPL-3.0-or-later.

## Commands

```bash
uv sync                                                    # Python dependencies (never pip)
uv run clif-dash build --config config/synth_10k.yaml      # build metric tables (~80 s on synthetic 10k)
uv run clif-dash serve --config config/synth_10k.yaml      # API + UI at http://127.0.0.1:8000
uv run pytest                                              # all tests (one shared build, ~80 s)
uv run pytest tests/test_server.py::test_timelines_match_upstream   # a single test

cd web && npm install && npm run build                     # bundle the UI into web/dist (npm run watch to rebuild on change)
```

`serve` needs a finished build and a built UI; without `web/dist` it serves only the API (docs at `/api/docs`). The shell may export `VIRTUAL_ENV` pointing at another project's venv; `uv` warns and still uses this project's `.venv`.

## Architecture

Three layers, and the metric logic in the first two is upstream's, copied into `src/clif_dash/upstream/` and changed only where marked `clif_dashboards:`. Keep it that way: the tests compare against upstream's output, and a silent edit to that code breaks the comparison's meaning.

**Build** (`src/clif_dash/build.py`, `stage.py`). Upstream read every table whole into pandas (15 GB peak on 10K synthetic hospitalizations). The build instead:
1. Reads `patient`, `hospitalization` and `adt` whole and stitches encounters across the site.
2. Phase A: DuckDB stages `respiratory_support` for adult encounters into per-chunk parquet partitions (`output/<site>/stage/phase_a/`). Worker processes run the clifpy waterfall, IMV runs and episodes, and the cohort exclusions per chunk.
3. Phase B: DuckDB stages the large tables only for encounters left in the cohort, in smaller chunks (`stage/phase_b/`). Workers compute per-episode metrics and proning rows.
4. Whole-cohort steps run last: the site-fitted mortality model, expected proning (its BMI imputation uses the cohort median), and data-quality counts, which DuckDB takes over the full source tables.

Two invariants make chunked results equal upstream's. Staged rows keep their source-file position (`source_row`) and are reloaded in that order, because upstream takes `first` values after merges and sorts. And any statistic upstream computed over a whole table is computed once and passed to chunks; the only one so far is the FiO2-recorded-as-percent test in `apply_outliers`.

Outputs in `output/<site>/`: `episodes.parquet` (one row per IMV episode with numerator/denominator columns for every measure), `proning.parquet`, `paralytic_days.parquet`, `summary.json` (meta, decisions, funnels, data quality), and `stage/`, which the server reads for timelines. Every file here is PHI.

**Server** (`src/clif_dash/server/`). FastAPI. `store.py` holds the episode and proning tables in memory and applies the shared filters (month range, hospital, ICU, age band, sex, race/ethnicity, code status, tracheostomy). `registry.py` lists each measure's columns, aggregation kind and "missed" rule, ported from upstream `web/src/metrics.js`; titles, units, targets and thresholds stay in `config/decisions.yaml`. `aggregate.py` ports upstream's pooled-ratio, mean and median arithmetic and p-chart limits. `timeline.py` builds one episode's hourly timeline on request by reading only that patient's rows from the staged chunk and calling upstream's `timelines()`. One endpoint per page: `/api/meta`, `/api/overview`, `/api/metric/{id}` (LTVV threshold in the id, e.g. `ltvv@7`), `/api/units`, `/api/episodes` (paginated, plus `/api/episodes.csv`), `/api/timeline/{episode_id}`, `/api/quality`.

**UI** (`web/src`). Upstream's React pages and charts, fetching from the API instead of reading embedded data. `format.js` keeps only display helpers (formatting, target status). Chart series colors are the Okabe-Ito palette in `styles.css`.

`config/decisions.yaml` is the single source of every analytic choice; the build reads values by `id`, and the UI shows the same file in each measure's "How this is calculated" panel. Never hard-code a threshold in Python or JS.

## Tests

`tests/oracle/` holds upstream's own output on the synthetic data: `upstream_synth10k.json` (funnels, episode and proning rows, data quality), `upstream_synth10k_timelines.json` (26 episodes), the config that produced them (its `decisions_file` and `output_html` point into a deleted scratch clone of upstream; repoint them before rerunning upstream), and a pristine copy of upstream `metrics.js`. `test_oracle.py` checks the build reproduces the rows and funnels; `test_server.py` checks the API's aggregates against upstream `metrics.js` run under node, and its timelines against upstream's. Any change to metric logic should move these tests deliberately, never incidentally.

## Never touch real data

Site configs (`config/*.yaml` other than `site_template.yaml` and `synth_10k.yaml`, all gitignored) point at real CLIF databases. Never build, serve, test, or read files against them, and never read anything under a site's `output_dir`: those are PHI. Develop and debug only against the synthetic data; the maintainer runs site builds and reports problems in sanitized form.

## Synthetic data

`~/Research/synthetic_clif/synth_clif_10k` (10,000 hospitalizations, 2018-2024, generated by `~/Research/synthetic_clif`). Use it for all development; never point tests at real UCSF data. It departs from CLIF in ways that hide bugs:
- Categories use display case (`IMV`, `Assist Control-Volume Control`, `RASS`, `Full`), so compare categories case-insensitively.
- There are no `sat_delivery_pass_fail` or `sbt_delivery_pass_fail` assessments and no paralytic infusions, so the documented SAT/SBT and paralytic measures cannot be validated.
- Only 12 hospitalizations end in `Expired`, so the mortality model is barely estimable.
- Only 1 encounter meets the PROSEVA ABG criteria, so the proning measures are effectively untestable.
- 1,105 of 2,400 ICU episodes drop as outside-hospital intubations, far more than real data would.

Report such gaps rather than fabricating inputs. CLIF stores datetimes in UTC; days and months are grouped in the site time zone from the config.

## Performance baseline (synthetic 10k)

Upstream: 3.7 min, 15 GB peak memory, 30 MB HTML (95% timelines). This build: 80 s with 4 workers, under 3 GB in the main process. Overview requests take ~0.25 s; a timeline 0.05-0.2 s, then cached per episode.
