# Modular dashboards: design plan

Branch: `modular-dashboards`. Status: Phase 0 done; Phases 1-3 proposed.

## Goal

The repository serves one dashboard, respiratory failure on invasive ventilation. We want the same build and server to carry several: sepsis practice and outcomes, antibiotic stewardship, and later device-associated infections. Each dashboard should be a module that declares its cohort, its measures and its decisions. The platform should own everything the dashboards share: site configuration, encounter stitching, chunked staging, filtering, aggregation, the API and the generic pages.

The respiratory dashboard becomes the first module. Its numbers must not move while it does.

## What is platform and what is respiratory today

Most of the code is already generic in shape, but respiratory assumptions run through files that look like platform code. These are the places a second dashboard would collide with:

| File | Respiratory assumptions |
|---|---|
| `stage.py` | `HOSPITALIZATION_KEYED`, `PATIENT_KEYED` and `REQUIRED_TABLES` list the tables the respiratory measures read. |
| `build.py` | `EPISODE_COLUMNS`, `COMPLETENESS_FLAGS`, `phase_a_chunk`, `phase_b_chunk`, the funnel wording, `fio2_recorded_as_percent`, and half of `data_quality` (semi-prone names, SAT/SBT rows). |
| `config.py` | One `decisions_file` and one `output_dir` per site. |
| `config/decisions.yaml` | `shared:` mixes site-wide choices (`age_min`, `stitch_hours`, `time_zone`, `small_cell_n`, `outliers`) with ventilation choices (`imv_source`, `episode_gap_hours`, `require_icu`, `exclude_outside_intubation`, `unit_attribution`, `time_attribution`, `comfort_care`). |
| `server/store.py` | `FILTER_FIELDS` includes `code_status`; the `trach` filter is hard-coded; display conversion assumes `start` and `end`. |
| `server/registry.py` | All of it: `METRICS`, `DOMAINS`, `STRATA`, `COMPLETENESS`, `INDEPENDENT_DENOMINATORS`, and the `ltvv@` route syntax in `resolve`. |
| `server/app.py` | Routes without a dashboard prefix, `DERIVED_SORT`, the LTVV special cases in `episode_selection`, and the timeline endpoint. |
| `server/timeline.py` | All of it. |
| `web/src/app.jsx` | The hard-coded title, the nav list, hash routes without a dashboard prefix, and the `Timeline` lanes. `Overview`, `MetricPage`, `Units`, `Patients` and `Quality` are nearly generic already, because they read everything from `/api/meta`. |
| `cli.py` | `build` and `serve` take no dashboard argument. |

The genuinely shared parts are encounter stitching, chunk assignment, `stage_table` and `load_staged_chunk` with their source-row ordering, `run_chunks`, row counts, `Filters`, `aggregate.py`, `jsonable`, and the summary writer.

## Target layout

The top-level `clif_dash` package is the platform; modules live under `clif_dash/dashboards/`. Phase 0 built the Python half of this:

```
src/clif_dash/
  config.py            site config
  stage.py             chunked staging, source-row order
  build.py             driver: encounters, site statistics, the dashboard's build, data quality, summary
  dashboard.py         the Dashboard contract, SiteBuild, BuildResult, Metric
  server/              app factory, store, filters, aggregate
  upstream/            upstream's code, vendored; both layers import it
  dashboards/
    __init__.py        DASHBOARDS by id, DEFAULT_DASHBOARD
    respiratory/
      __init__.py      RESPIRATORY = Dashboard(...)
      build.py         phase A (waterfall, IMV episodes), phase B (metrics, proning), whole-cohort steps
      registry.py      measures, domains, strata, completeness flags, ltvv@ resolution
      timeline.py      hourly timeline, the dashboard's detail view
```

Later phases add:

```
config/
  platform.yaml        site-wide decisions: age_min, stitch_hours, time_zone, small_cell_n, outliers (Phase 1)
src/clif_dash/dashboards/respiratory/decisions.yaml   (Phase 1)
web/src/
  app.jsx              shell, dashboard picker, generic pages
  dashboards/
    index.js           id -> { Detail component }
    respiratory/Timeline.jsx
```

`upstream/` stays where it is. The platform uses it too: encounter stitching (`prep.build_encounters`), clifpy loading, outlier removal and the Charlson index are site-wide steps, not respiratory ones. Moving it into the respiratory module would make the platform import from a module.

## The Dashboard contract

A module exports one `Dashboard` object (`src/clif_dash/dashboard.py`). The platform calls it and imports nothing else from the module.

```python
@dataclass(frozen=True)
class Dashboard:
    id: str
    title: str
    tables: tuple[str, ...]            # CLIF tables read beyond patient, hospitalization, adt
    required_tables: tuple[str, ...]
    site_statistics: Callable          # (con, cfg) -> dict: statistics over whole source tables
    build: Callable                    # (SiteBuild) -> BuildResult
    data_quality: Callable             # (con, cfg) -> dict, merged with the platform's counts
    metrics: list[Metric]
    domains: list[str]
    strata: list[tuple[str, str]]
    completeness: list[tuple[str, str]]
    independent_denominators: frozenset[str]
    resolve: Callable                  # route id -> (metric, threshold, default)
    detail: Callable | None = None     # (store) -> builder with .build(row_id); respiratory: timelines
```

The platform stitches encounters, calls `site_statistics`, and hands the module a `SiteBuild`: the config, a DuckDB connection, the stitched encounters, mapping and ADT, the site statistics, and two helpers, `stage(table, chunk_map, stage_name)` and `run_chunks(function, chunks)`. The module's `build` returns a `BuildResult`: named row tables, which the platform writes as parquet, and its parts of `summary.json`.

Three choices in this contract need stating.

The module orchestrates its own phases. An earlier draft had separate cohort, measure and finish hooks, but respiratory's work between phases (re-sorting the waterfall into phase B chunks) fits none of them, and a stewardship dashboard needs only one phase. The platform supplies staging and worker pools; the module decides how many passes to make.

`site_statistics` turns the build's second invariant into a hook. The FiO2-recorded-as-percent test is the one statistic upstream computed over a whole table. A module that computes such a statistic inside a chunk gets results that depend on chunk size, so the platform computes these before any chunk runs and passes them to every chunk function. `run_chunks` spawns worker processes, so chunk functions must be module-level functions.

Decisions will load in two layers. In Phase 1 the platform will read `config/platform.yaml` and merge its entries into each module's `shared:` list before module code sees them, so the respiratory module receives the same `decisions` dict as now and the "How this is calculated" panel still shows the site-wide choices. Until then the platform reads `age_min` and `stitch_hours` from the one `decisions.yaml`. The split waits for Phase 1 because it changes the site YAML's `decisions_file` key, and Phase 1 changes the site YAML anyway.

Phase 1 also adds row-table descriptions (ID column and unit label for each table), which replace the word "episode" in the server. Respiratory rows are IMV episodes; sepsis rows will be encounters; stewardship rows will be patient-days.

## Outputs

```
output/<site>/
  encounters/          encounters, mapping, adt (shared by all dashboards)
  respiratory/         episodes.parquet, proning.parquet, paralytic_days.parquet, summary.json, stage/
  sepsis/              ...
```

Each dashboard stages its own tables for its own cohort, so two dashboards that both read `vitals` stage it twice. That costs build time, not correctness, and a shared staging cache can come later if builds get slow.

## API and UI

`/api/dashboards` lists the dashboards whose builds exist. Every current route moves under a prefix: `/api/{dashboard}/meta`, `/overview`, `/metric/{id}`, `/units`, `/rows/{table}`, `/rows/{table}.csv`, `/detail/{row_id}`, `/quality`. The UI's hash routes gain the same segment (`#/respiratory/metric/ltvv@7`), the header title comes from `meta`, and a landing page lists the dashboards. The filter rail already builds itself from `meta.filter_fields`; it only needs the module's extra fields. A dashboard with no `detail` builder gets no detail page.

## Phases

**Phase 0 (done): extract the respiratory module, change no behavior.** The build's respiratory phases, the measure registry and the timeline builder moved into `dashboards/respiratory/`; `build.py`, `stage.py` and `server/` lost their respiratory table lists and measures. Routes, output paths and the site YAML are unchanged. `test_oracle.py` and `test_server.py` pass unmodified (one import line in `test_build_units.py` moved with `drop_untimed_rows`), and the three parquet outputs and `summary.json` (less its timestamp) are identical to a build from `main`. Still respiratory-shaped and deliberately left for later phases: `Store`'s `episodes` and `proning` attributes, `DERIVED_SORT`, the `ltvv@` cases in `app.py`, and the `trach` and `code_status` filters.

**Phase 1: namespace the API and UI.** Add the dashboard prefix to routes and hash URLs, `/api/dashboards`, the landing page, the `--dashboard` option on `build` and `serve`, per-dashboard output folders, the decisions split, and row-table descriptions. `test_server.py` changes only its URLs. Write a stub second module (one trivial measure over all ICU encounters) to prove that two dashboards build and serve side by side, and delete it once a real module replaces it.

**Phase 2: generalize what the second module will need.** Four changes, each with its own tests:

- A `rate` aggregation kind: scale × Σevents / Σexposure (per 1,000 patient-days or device-days), with u-chart limits. Infection and stewardship measures are rates, and the current p-chart is wrong for them.
- Module-declared filters, so `trach` and `code_status` leave the platform.
- Sorting and threshold variants from the registry. `DERIVED_SORT` and the `ltvv@` syntax become properties of a measure (its numerator and denominator; a list of parameter values), not code in `app.py`.
- Small-cell suppression on the server. Today the server sends raw counts and `small_cell_n`, and the browser decides what to hide. With several dashboards and wider hosting, suppression belongs in one place, before the response leaves the server.

None of these may move a respiratory number; the oracle tests guard that.

**Phase 3: the second real module.** See the next section for which one.

## Which dashboard comes second

CLABSI and CAUTI should not come second, because CLIF cannot supply their denominators. Both are rates per device-day, and device-days need line and catheter insertion and removal times. The synthetic data has no lines-and-drains table, and as far as I know neither does CLIF 2.1. `patient_procedures` carries billed codes with one timestamp and no removal; `intake_output` has a free-text `fluid_name` and no catheter category; `invasive_hemodynamics` implies an arterial or central line only while readings are charted. Proxies built from these would be guesses about line presence. The honest route is to propose a lines-and-drains table to the CLIF consortium, or to check what UCSF's own extract holds, before designing these measures.

Antibiotic stewardship is the better second module. Days of therapy per 1,000 patient-days needs only `medication_admin_intermittent`, ADT and the encounter table, all present in the synthetic data with 1.2 million administrations. Its clinical logic is simple, so a wrong number points at the platform rather than the definitions. It also exercises exactly what Phase 2 adds: a rate measure, a unit of analysis that is not an episode, and a dashboard with no timeline.

Sepsis should come third. A CDC Adult Sepsis Event definition draws on `microbiology_culture`, `medication_admin_intermittent`, `labs`, vasopressors and `respiratory_support`, all present in the synthetic data, but the definition carries many decisions (culture-antibiotic windows, qualifying antibiotic days, organ dysfunction criteria) that deserve their own review.

New modules have no upstream oracle. Each needs a small set of hand-built encounters with known answers, written as unit tests, plus a check that its measures are plausible on the synthetic data. Where the synthetic data cannot exercise a measure, the module's tests should say so rather than inventing inputs.

## Risks

The `source_row` ordering invariant lives in `stage.py` and stays there; modules that take `first` values after merges inherit the same protection. The respiratory module must keep `phase_a` and `phase_b` chunk sizes configurable separately, as now. Site configs gain a `dashboards:` list; old configs without it should default to `[respiratory]` so the maintainer's existing site YAML keeps working.
