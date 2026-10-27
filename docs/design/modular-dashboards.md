# Modular dashboards: design plan

Branch: `modular-dashboards`. Status: Phases 0 to 3 done: respiratory and antimicrobial stewardship dashboards.

## Goal

The repository serves one dashboard, respiratory failure on invasive ventilation. We want the same build and server to carry several: sepsis practice and outcomes, antibiotic stewardship, and later device-associated infections. Each dashboard should be a module that declares its cohort, its measures and its decisions. The platform should own everything the dashboards share: site configuration, encounter stitching, chunked staging, filtering, aggregation, the API and the generic pages.

The respiratory dashboard becomes the first module. Its numbers must not move while it does.

## Where respiratory assumptions lived before Phase 0

Most of the code was already generic in shape, but respiratory assumptions ran through files that looked like platform code. Phase 0 removed those in `stage.py`, `build.py`, `registry.py` and `timeline.py`; the rest remain for Phases 1 and 2.

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

The top-level `clif_dash` package is the platform; modules live under `clif_dash/dashboards/`. After Phase 1:

```
config/
  site_template.yaml   site config: paths, dashboards list, decisions folder
  decisions/
    platform.yaml      site-wide decisions: age_min, stitch_hours, outliers, time_zone
    respiratory.yaml   the respiratory dashboard's decisions
    icu_stays.yaml     the platform-test dashboard's decisions
src/clif_dash/
  config.py            site config, SiteConfig.bind(dashboard), two-layer decisions
  stage.py             chunked staging, source-row order
  build.py             driver: stitch once, then per dashboard: site statistics, build, data quality, summary
  dashboard.py         the Dashboard contract, RowTable, SiteBuild, BuildResult, Metric
  server/              app factory (a router per dashboard), store, filters, aggregate
  upstream/            upstream's code, vendored; both layers import it
  dashboards/
    __init__.py        DASHBOARDS by id, DEFAULT_DASHBOARD
    respiratory/
      __init__.py      RESPIRATORY = Dashboard(...)
      build.py         phase A (waterfall, IMV episodes), phase B (metrics, proning), whole-cohort steps
      registry.py      measures, domains, strata, completeness flags, ltvv@ resolution
      timeline.py      hourly timeline, the dashboard's detail view
    icu_stays/
      __init__.py      platform test: one row per adult encounter with ICU time
web/src/
  app.jsx              dashboard loader and landing page, shell, generic pages, respiratory list and timeline
```

Decision files stay in `config/` rather than moving into the package, because sites copy and edit them. The respiratory list columns and timeline stay in `app.jsx`, registered by dashboard id (`LISTS`, the `detail` flag in `meta`); splitting them into `web/src/dashboards/` can wait until a second dashboard brings its own.

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
    filters: tuple[FilterField, ...]   # added after the shared hospital, ICU, age, sex, race/ethnicity filters
    completeness: list[tuple[str, str]]
    independent_denominators: frozenset[str]   # denominators that get p- or u-chart limits
    row_tables: tuple[RowTable, ...]   # primary first: name, ID column, unit label
    detail: Callable | None = None     # (store) -> builder with .build(row_id); respiratory: timelines
```

The platform stitches encounters, calls `site_statistics`, and hands the module a `SiteBuild`: the config, a DuckDB connection, the stitched encounters, mapping and ADT, the site statistics, and two helpers, `stage(table, chunk_map, stage_name)` and `run_chunks(function, chunks)`. The module's `build` returns a `BuildResult`: named row tables, which the platform writes as parquet, and its parts of `summary.json`.

Three choices in this contract need stating.

The module orchestrates its own phases. An earlier draft had separate cohort, measure and finish hooks, but respiratory's work between phases (re-sorting the waterfall into phase B chunks) fits none of them, and a stewardship dashboard needs only one phase. The platform supplies staging and worker pools; the module decides how many passes to make.

`site_statistics` turns the build's second invariant into a hook. The FiO2-recorded-as-percent test is the one statistic upstream computed over a whole table. A module that computes such a statistic inside a chunk gets results that depend on chunk size, so the platform computes these before any chunk runs and passes them to every chunk function. `run_chunks` spawns worker processes, so chunk functions must be module-level functions.

Decisions load in two layers. A config bound to a dashboard reads `config/decisions/platform.yaml`, then the dashboard's own file, and puts the site-wide entries first under `shared:`, so the respiratory module receives the same decisions as before the split and the "How this is calculated" panel still shows the site-wide choices. A site config names the folder with `decisions_dir` and may point any file elsewhere under `decisions:`. The old `decisions_file` key keeps working: an existing file serves as the respiratory file, and any site-wide entry in it must match `platform.yaml` or the build stops and names it; the old shipped path falls back to the defaults with a note; any other missing path is an error. A dashboard file cannot change a site-wide value, because encounters are stitched once for every dashboard.

Row tables replace the word "episode" in the server. Each names its file, ID column and unit ("IMV episode", "ICU stay"); the first is the primary table, whose rows the list page shows and the detail view opens. Any other table must carry the primary table's ID column. Respiratory rows are IMV episodes; sepsis rows will be encounters; stewardship rows will be patient-days.

## Outputs

```
output/<site>/
  respiratory/         episodes.parquet, proning.parquet, paralytic_days.parquet, summary.json, stage/
  icu_stays/           stays.parquet, summary.json, stage/
```

One `build` invocation stitches encounters once and builds every dashboard in the site's `dashboards:` list (or those named with `--dashboard`). Each dashboard keeps its own copy of the stitched encounters, mapping and ADT in its stage folder, because its detail view reads the mapping it was built with; a shared folder would let a later build of one dashboard, on changed source data, silently break another's timelines. Each dashboard also stages its own tables for its own cohort, so two dashboards that both read `vitals` stage it twice. That costs build time, not correctness, and a shared staging cache can come later if builds get slow.

## API and UI

`/api/dashboards` lists the dashboards whose builds exist. Every route sits under a prefix: `/api/{dashboard}/meta`, `/overview`, `/metric/{id}`, `/units`, `/rows`, `/rows.csv`, `/detail/{row_id}`, `/quality`. `/rows` lists the primary table. The UI's hash routes carry the same segment (`#/respiratory/metric/ltvv@7`); a hash without one, from an old bookmark, gets the first dashboard's id. The header title and page labels come from `meta`, and a landing page lists the dashboards when there are several. The filter rail offers only the shared filters the dashboard's rows carry. A dashboard with no `detail` builder gets no Timeline tab.

`/overview` counts each table as `n_<table>`, which keeps the `n_episodes` and `n_proning` keys `test_server.py` reads. Respiratory-specific page copy also remains in the UI (the data-quality notes, the observed/expected explanations); it shows only when a dashboard's data carry those fields.

## Phases

**Phase 0 (done): extract the respiratory module, change no behavior.** The build's respiratory phases, the measure registry and the timeline builder moved into `dashboards/respiratory/`; `build.py`, `stage.py` and `server/` lost their respiratory table lists and measures. Routes, output paths and the site YAML are unchanged. `test_oracle.py` and `test_server.py` pass unmodified (one import line in `test_build_units.py` moved with `drop_untimed_rows`), and the three parquet outputs and `summary.json` (less its timestamp) are identical to a build from `main`. Still respiratory-shaped and deliberately left for later phases: `Store`'s `episodes` and `proning` attributes, `DERIVED_SORT`, the `ltvv@` cases in `app.py`, and the `trach` and `code_status` filters.

**Phase 1 (done): namespace the API and UI.** Per-dashboard output folders, the `dashboards:` list and `--dashboard` on `build` and `serve`; the decisions split; a router per dashboard under `/api/{id}`; row-table descriptions and a store that loads whatever tables a dashboard declares; `icu_stays`, a platform-test dashboard built from encounters and ADT alone; and a UI that routes by dashboard. `test_server.py` changed only its URLs, the respiratory outputs still match the Phase 0 baseline (apart from the order of shared decisions and one changelog entry), and new tests cover the decisions rules and two dashboards served side by side. Both dashboards were checked in a browser on the synthetic build. Delete `icu_stays` once a real second dashboard replaces it.

**Phase 2: generalize what the second module will need.** Four changes, each with its own tests:

- Done: a `rate` kind, scale × Σevents / Σexposure (per 1,000 patient-days or device-days), with u-chart limits; a rate's n counts the rows contributing exposure. Control limits now follow the kind: p-chart for a ratio, u-chart for a rate. `icu_stays` carries a test rate, deaths per 1,000 ICU days.
- Done: dashboard-declared filters. The platform offers hospital, ICU, age band, sex and race/ethnicity; respiratory adds code status and the tracheostomy flag as `FilterField`s, either a multi-select over a column's values or a yes/no flag.
- Done: variants and sorting from the measure. A measure declares `Variants` (the decision holding the default, the summary key listing built values, the numerator column per value), and `resolve_route` reads `<measure>@<value>` for any measure; missed rules receive the resolved measure; the list sorts by any pooled measure's per-row share. `DERIVED_SORT`, the dashboards' resolvers and the `ltvv_*` response keys are gone.
- Dropped: small-cell suppression on the server. The dashboards are for internal use only, by a site's own QI team, who already see patient-level rows; no PHI leaves a site (decided 2026-10-20). Small groups are no longer greyed or flagged either: the n < 11 cutoff is a privacy convention, not a statistical one, and was removed. If the consortium later pools results across sites, suppression belongs in that export step (an aggregate-only file with small cells removed and totals protected from back-calculation), not in the internal server.

None of these moved a respiratory number; the oracle tests still pass unchanged. The server also refuses to start on a build older than its dashboard's measures, which would otherwise show a blank page.

**Phase 3 (done): the stewardship dashboard.** `dashboards/stewardship/` computes NHSN AU-style days of therapy per 1,000 days present for all antibacterials and the seven adult SAAR agent groups (2017/2018 baseline, transcribed from the CDC guide into `config/decisions/stewardship.yaml` as CLIF `med_category` lists), plus the share of days present on any antibacterial. Rows are one encounter's days in one ICU at one hospital in one month, so each day falls in its own month; the store now refuses a primary table whose IDs repeat. Days present come from ICU ADT rows; administrations are staged with a row condition (only counted agents), placed in the ICU row that contains them, and counted once per agent per local day per unit. These are not SAARs: there are no predicted values, so they support comparison within the site only. No measure gets control limits, because days of therapy cluster within patients.

There is no upstream oracle, so `tests/test_stewardship.py` builds a five-hospitalization CLIF site by hand and checks counts worked out by hand: repeat doses, two agents, not-given doses, ward doses, a dose whose UTC date differs from its local date, oral vancomycin (all antibacterials, not anti-MRSA), a same-day ICU transfer, a stay leaving at midnight, a month boundary, an open ICU row, and a child. On the synthetic data the build takes about a second and gives 422 antibacterial days of therapy per 1,000 days present; the synthetic data has only cefepime, meropenem, piperacillin/tazobactam and IV vancomycin, so the community, narrow-spectrum and antifungal groups are zero there and are tested only by the fixture. The `icu_stays` platform test is deleted; the two-dashboard tests now use stewardship.

CLIF gaps the stewardship measures inherit: no respiratory route category (NHSN counts inhaled agents), no gemifloxacin category, and a combined micafungin/posaconazole/voriconazole category that cannot be split, so it is left out of the antifungal group.

## Why stewardship came second (the choice made before Phase 3)

CLABSI and CAUTI should not come second, because CLIF cannot supply their denominators. Both are rates per device-day, and device-days need line and catheter insertion and removal times. The synthetic data has no lines-and-drains table, and as far as I know neither does CLIF 2.1. `patient_procedures` carries billed codes with one timestamp and no removal; `intake_output` has a free-text `fluid_name` and no catheter category; `invasive_hemodynamics` implies an arterial or central line only while readings are charted. Proxies built from these would be guesses about line presence. The honest route is to propose a lines-and-drains table to the CLIF consortium, or to check what UCSF's own extract holds, before designing these measures.

Antibiotic stewardship is the better second module. Days of therapy per 1,000 patient-days needs only `medication_admin_intermittent`, ADT and the encounter table, all present in the synthetic data with 1.2 million administrations. Its clinical logic is simple, so a wrong number points at the platform rather than the definitions. It also exercises exactly what Phase 2 adds: a rate measure, a unit of analysis that is not an episode, and a dashboard with no timeline.

Sepsis should come third. A CDC Adult Sepsis Event definition draws on `microbiology_culture`, `medication_admin_intermittent`, `labs`, vasopressors and `respiratory_support`, all present in the synthetic data, but the definition carries many decisions (culture-antibiotic windows, qualifying antibiotic days, organ dysfunction criteria) that deserve their own review.

New modules have no upstream oracle. Each needs a small set of hand-built encounters with known answers, written as unit tests, plus a check that its measures are plausible on the synthetic data. Where the synthetic data cannot exercise a measure, the module's tests should say so rather than inventing inputs.

## Risks

The `source_row` ordering invariant lives in `stage.py` and stays there; modules that take `first` values after merges inherit the same protection. The respiratory module must keep `phase_a` and `phase_b` chunk sizes configurable separately, as now. Site configs gain a `dashboards:` list; old configs without it defaulted to `[respiratory]` until 2026-10-27, and now default to every dashboard (respiratory, stewardship, microbiology).
