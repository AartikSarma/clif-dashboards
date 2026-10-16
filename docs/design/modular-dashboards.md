# Modular dashboards: design plan

Branch: `modular-dashboards`. Status: proposal, nothing implemented.

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

```
src/clif_dash/
  platform/
    config.py          site config; list of enabled dashboards
    encounters.py      stitching, adults, ADT (built once per site)
    stage.py           chunked staging, source-row order
    build.py           driver: encounters, then each dashboard's phases
    dashboard.py       the Dashboard contract below
    server/            app factory, store, filters, aggregate, generic routes
  dashboards/
    respiratory/
      __init__.py      DASHBOARD = Dashboard(...)
      build.py         phase A (waterfall, IMV episodes), phase B (metrics, proning), finish
      registry.py      measures, domains, strata, completeness flags
      detail.py        hourly timeline (today's server/timeline.py)
      decisions.yaml
      upstream/        moved verbatim from src/clif_dash/upstream
config/
  platform.yaml        site-wide decisions: age_min, stitch_hours, time_zone, small_cell_n, outliers
web/src/
  app.jsx              shell, dashboard picker, generic pages
  dashboards/
    index.js           id -> { Detail component }
    respiratory/Timeline.jsx
```

`upstream/` imports only relative to itself (`from . import prep`), so moving the folder edits no line in it. That keeps the oracle comparison meaningful.

## The Dashboard contract

A module exports one object. The platform calls its hooks and never imports module internals.

```python
@dataclass(frozen=True)
class Dashboard:
    id: str                       # "respiratory": URL segment and output folder
    title: str
    decisions_file: Path
    tables: TableNeeds            # required and optional tables, keyed by hospitalization or patient
    row_tables: dict[str, RowTable]   # e.g. {"episodes": RowTable(id_column="episode_id", unit="IMV episode"), "proning": ...}
    site_statistics: Callable     # (con, cfg) -> dict: whole-table statistics computed once, passed to every chunk
    cohort_chunk: Callable | None # phase A: find the cohort in one chunk; None means every adult encounter
    measure_chunk: Callable       # phase B: compute rows for one chunk
    finish: Callable              # whole-cohort steps (models fitted on the full cohort, funnels)
    data_quality: Callable        # (con, cfg) -> dict, merged with the platform's row counts
    metrics: list[Metric]
    domains: list[str]
    strata: list[tuple[str, str]]
    filter_fields: list[FilterField]  # added to the platform's hospital, unit, age band, sex, race/ethnicity
    completeness: list[tuple[str, str]]
    detail: DetailBuilder | None  # optional per-row view (respiratory: the timeline)
```

Three choices in this contract need stating.

`site_statistics` turns the build's second invariant into a hook. Today the FiO2-recorded-as-percent test is the one statistic upstream computed over a whole table; it is a special case in `build.py`. Every module will have its own, and a module that computes one inside a chunk gets results that depend on chunk size. Making it a named hook puts the rule where the next author will see it.

`row_tables` replaces the word "episode" in the platform. Respiratory rows are IMV episodes; sepsis rows will be encounters; stewardship rows will be patient-days or unit-months. The platform asks the module for the table name, its ID column and the label to print.

Decisions load in two layers. The platform reads `config/platform.yaml`, and the loader merges its entries into each module's `shared:` list before any module code sees them. The respiratory module therefore receives the same `decisions` dict it receives today, and the "How this is calculated" panel still shows the site-wide choices.

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

**Phase 0: extract the respiratory module, change no behavior.** Move code into the layout above and write the contract, keeping today's routes and output paths. The acceptance test is that `test_oracle.py` and `test_server.py` pass with their assertions untouched; only import paths and the build fixture's output path may change. Nothing in this phase may move a number. Commit the `upstream/` move on its own, so `git log --follow` shows it as a pure rename.

**Phase 1: namespace the API and UI.** Add the dashboard prefix to routes and hash URLs, `/api/dashboards`, the landing page, and the `--dashboard` option on `build` and `serve`. `test_server.py` changes only its URLs. Write a stub second module (one trivial measure over all ICU encounters) to prove that two dashboards build and serve side by side, and delete it once a real module replaces it.

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
