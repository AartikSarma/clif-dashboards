# Adding a dashboard

A dashboard is a folder of Python under `src/clif_dash/dashboards/<id>/` plus one decisions file, `config/decisions/<id>.yaml`. The platform already does everything dashboards share: it reads the site config, links hospitalizations into encounters, stages large tables in chunks, runs worker processes, writes the outputs, and serves the API and every generic page (overview, measure pages, ICU and hospital comparison, row list, data quality). Your dashboard says which CLIF tables it reads, builds one table of rows with a numerator and denominator column per measure, and lists its measures. The stewardship dashboard (`src/clif_dash/dashboards/stewardship/`) is the best worked example: it is short, has one build phase, and has a hand-counted test.

Read this whole page before starting. The steps are in the order you will need them.

## Before you write code

**Define the measures on paper first.** For each measure, write the numerator, the denominator, the unit of analysis (an encounter, an episode, a patient-day), the time attribution (which month a row counts in), and the source of each choice (a guideline, a consortium study, a local decision). Every one of these becomes an entry in your decisions file, and reviewers will read that file before the code.

**Check that CLIF can supply the denominator.** Some measures cannot be computed from CLIF 2.1. CLABSI and CAUTI, for example, need line and catheter days, and CLIF has no table that records insertion and removal. Say so in your proposal rather than building on a proxy.

**Develop only on synthetic data.** Use the [synthetic_clif](https://github.com/Common-Longitudinal-ICU-data-Format/synthetic_clif) dataset (see the README's "Try it without real data"). Never put real patient data in code, tests, issues or pull requests, and never paste rows, IDs, dates or small counts from your site.

**Open an issue describing the dashboard** before a large pull request, so the measure definitions can be discussed while they are cheap to change.

## Step 1. Create the module

```
src/clif_dash/dashboards/<id>/
  __init__.py     the Dashboard object and its measures
  build.py        how rows are built
config/decisions/<id>.yaml
tests/test_<id>.py
```

Use a short lower-case `id` such as `sepsis`. It becomes the output folder (`output/<site>/<id>/`), the API prefix (`/api/<id>/`), the URL segment (`#/<id>/overview`) and the decisions file name.

## Step 2. Write the build

`build.py` exports three functions the platform calls, plus whatever chunk functions you need.

```python
from ... import stage
from ...config import SiteConfig
from ...dashboard import BuildResult, SiteBuild, assign_chunks, log
from ...upstream import prep            # unit_label, race_eth, age_band, shared with the other dashboards

TABLES = ("medication_admin_intermittent", "labs")    # CLIF tables you read, beyond patient/hospitalization/adt
REQUIRED_TABLES = ("medication_admin_intermittent",)  # the build stops if these are missing


def site_statistics(con, cfg: SiteConfig) -> dict:
    """Statistics over whole source tables, computed once before any chunk runs."""
    return {}


def data_quality(con, cfg: SiteConfig) -> dict:
    """Counts over whole source tables for the Data quality page (DuckDB SQL on cfg.table_path(...))."""
    return {}


def my_chunk(cfg: SiteConfig, chunk: int, statistics: dict) -> dict:
    """Runs in a worker process for one chunk of encounters."""
    rows = stage.load_staged_chunk(cfg, "my_stage", "labs", chunk)
    ...
    return {"rows": per_encounter_rows}


def build(site: SiteBuild) -> BuildResult:
    cfg = site.cfg
    adults = site.encounters[site.encounters["adult"]]
    chunks = assign_chunks(adults["encounter_block"], cfg.encounters_per_chunk, cfg.workers)
    site.stage("labs", site.mapping.merge(chunks, on="encounter_block"), "my_stage",
               where="lower(CAST(source.lab_category AS VARCHAR)) IN ('lactate')")
    results = site.run_chunks(my_chunk, sorted(chunks["chunk"].unique()))
    rows = ...  # one DataFrame: one row per unit of analysis
    return BuildResult(tables={"my_rows": rows},
                       summary={"funnel": [{"step": "Adults", "n": len(adults)}, ...]},
                       meta={"months": sorted(rows["month"].unique().tolist())})
```

If your measures count events per ICU day, use `src/clif_dash/icu.py`, which every per-day dashboard shares: ICU intervals, days present, placing a timed event in the ICU stay that contains it, the analysis window, and the whole-month coverage rule. For a large table (labs, assessments, infusions), place its rows in DuckDB rather than staging them: inside `icu_session`, `placed_sql` gives each row's encounter, unit and local day, `infusion_runs_sql` and `interval_days_sql` give the ICU days a continuous infusion ran, and only your per-day summary reaches pandas. The glycemic dashboard (`src/clif_dash/dashboards/glycemic/`) is the shortest example.

What `SiteBuild` gives you:

- `site.cfg`: the site config, bound to your dashboard. `cfg.decisions()` returns your merged decisions; `cfg.stage_dir` is your scratch folder; `cfg.site_timezone` is the local zone; `cfg.start_date` and `cfg.end_date` are the optional analysis window.
- `site.encounters`: one row per linked encounter (`encounter_block`), with `hospitalization_id` (the first one), `patient_id`, `admission_dttm`, `discharge_dttm`, `age`, `adult`, `discharge_category`, `sex_category`, `race_category` and `ethnicity_category`.
- `site.mapping`: `hospitalization_id` → `encounter_block`.
- `site.adt`: every ADT row, with its `encounter_block`.
- `site.stage(table, chunk_map, stage_name, where=None)`: copies the rows of a large table for the encounters in `chunk_map` into per-chunk parquet files, keeping their source-file order. Use `where` to copy only the rows you need; on a large site this is the difference between copying a table and copying a slice of it.
- `site.run_chunks(function, chunks)`: calls `function(cfg, chunk, site_statistics)` for each chunk in parallel worker processes.

Rules that keep results correct at every site:

- **Chunk functions must be module-level functions.** Workers are separate processes; lambdas and closures cannot be sent to them.
- **Never compute a whole-table statistic inside a chunk.** A chunk sees only its own encounters, so a median, a mean or a "most common value" there depends on how the site was split. Compute it in `site_statistics` and read it from the `statistics` argument.
- **Compare CLIF categories case-insensitively.** Sites and the synthetic data differ in case (`IMV` and `imv`).
- **CLIF datetimes are UTC.** Convert with `cfg.site_timezone` before grouping by day or month.
- **Put every analytic choice in the decisions file** and read it by `id` (see step 4). Do not hard-code a threshold, a window or a list of agents in Python or JavaScript.

### The rows you return

`BuildResult.tables` maps a name to a DataFrame; each is written as `<name>.parquet`. The first table you declare in step 3 is the primary one. Its rows need:

| Column | Why |
| --- | --- |
| an ID column, unique per row | the row list, search and CSV download; the server refuses to start if IDs repeat |
| `month` (`YYYY-MM`, local time) | the month filters and every run chart |
| `unit` and `hospital` | the ICU and hospital comparison pages; label units with `prep.unit_label(location_type)` so they match the other dashboards |
| `sex`, `race_eth`, `age_band` | the shared filters and breakdowns; build them with `prep.race_eth` and `prep.age_band` |
| `label_id` | the hospitalization ID shown to users |
| one numerator and one denominator column per measure | what the server aggregates |

A second table (respiratory's proning cohort, for example) must carry the primary table's ID column. Any column holding time-zone-aware datetimes is shown in local time; `encounter_block` is hidden.

`summary["funnel"]` is the cohort funnel shown on each measure page: a list of `{"step": text, "n": count}` from all encounters down to your rows. `meta["months"]`, the sorted list of months in your rows, fills the month filter. Whatever `data_quality` and `BuildResult.data_quality` return is saved in `summary.json`; the Data quality page shows table row counts for every dashboard, and other counts only once the page is taught to display them (in the `Quality` component of `web/src/app.jsx`).

## Step 3. Declare the dashboard

`__init__.py` exports one `Dashboard`. Register it by adding it to `DASHBOARDS` in `src/clif_dash/dashboards/__init__.py`.

```python
from ...dashboard import Dashboard, FilterField, Metric, RowTable
from . import build

ROWS = RowTable("my_rows", "row_id", "ICU encounter", "ICU encounters")

METRICS = [
    Metric("lactate_3h", "my_rows", "ratio", "lactate_3h_num", "lactate_3h_den", "encounters",
           missed=lambda rows, metric: (rows["lactate_3h_den"] > 0) & (rows["lactate_3h_num"] == 0)),
    Metric("los", "my_rows", "median", "icu_los_days", den_label="encounters"),
]

MY_DASHBOARD = Dashboard(
    id="sepsis",
    title="ICU Sepsis Care",
    tables=build.TABLES,
    required_tables=build.REQUIRED_TABLES,
    site_statistics=build.site_statistics,
    build=build.build,
    data_quality=build.data_quality,
    row_tables=(ROWS,),
    metrics=METRICS,
    domains=["Recognition", "Treatment"],          # tile groups on the overview, in order
    strata=[("unit", "ICU"), ("hospital", "Hospital"), ("sex", "Sex"),
            ("race_eth", "Race / ethnicity"), ("age_band", "Age band")],
    filters=(),                                    # extra filters, e.g. FilterField("code_status", "Code status")
    completeness=[],                               # (flag column, label) inputs a measure depends on
    independent_denominators=frozenset({"encounters"}),
)
```

Measure kinds (`Metric.kind`):

| Kind | Value | Columns |
| --- | --- | --- |
| `ratio` | 100 × Σ numerator ÷ Σ denominator, over rows with a denominator | `numerator`, `denominator` (or `None` for one per row) |
| `rate` | `scale` × Σ events ÷ Σ exposure (deaths per 1,000 ICU days) | `numerator`, `denominator`, `scale` |
| `mean`, `median` | the mean or median of one column, with IQR | `numerator` is the column |

Other `Metric` fields:

- `den_label`: what the denominator counts, shown beside every value ("encounters", "days present").
- `missed`: a function `(rows, metric) -> boolean Series` choosing the rows the list page shows for "missed this measure". Set `missed_label` (for example `"with any"`) when "missed" reads wrongly.
- `expected`: a column of per-row expected probabilities, for observed/expected.
- `needs`: completeness flags (from `completeness`) this measure depends on.
- `variants`: for a measure computed at several thresholds, chosen in the URL as `<id>@<value>`; see respiratory's LTVV.

`heatmaps` adds a **Time of day** page: each `Heatmap(id, title, source, time_column, event, where=None, note="")` counts the rows of a table by the local hour (rows) and month (columns) of a time-zone-aware column, with `where` naming a column that must be true for a row to count. The flow dashboard's ICU admissions and transfers out are the example.

`independent_denominators` lists the `den_label`s whose units are independent enough for control limits (a p-chart for ratios, a u-chart for rates). Leave out anything clustered within patients, such as patient-days or ventilator hours, or the limits will be falsely narrow.

## Step 4. Write the decisions file

`config/decisions/<id>.yaml` has three sections. Site-wide decisions from `platform.yaml` are added in front of yours automatically.

```yaml
version: 1

shared:                     # choices that apply to every measure in the dashboard
  - id: time_zero
    label: Time zero
    value: ed_triage
    display: Time of ED triage, or hospital admission when there is no ED stay
    alternatives: [First antibiotic order]
    source: SEP-1

metrics:
  lactate_3h:               # one entry per Metric, keyed by its id
    number: 1               # shown on the tile
    domain: Recognition     # must match one of the Dashboard's domains
    title: Lactate measured within 3 h of time zero
    short: Lactate ≤ 3 h    # tile and table label
    unit: "%"               # "%", or a unit such as "days" or "per 1,000 days present"
    unit_short: null        # optional shorter unit for tiles, e.g. "/1k days"
    direction: higher       # higher, lower or neutral: which way is better
    target: 90              # or null
    numerator: Encounters with a lactate result within 3 h of time zero
    denominator: Adult ICU encounters meeting the sepsis definition
    decisions:              # choices specific to this measure
      - id: window_h
        label: Window
        value: 3
        display: Within 3 h of time zero
        alternatives: [6 h]
        source: SEP-1

changelog:
  - date: 2026-11-01
    change: Initial definitions
```

Every decision has `id`, `label`, `value`, `display`, `alternatives` and `source`. The build reads `value` by `id`; the page shows `display`, `alternatives` and `source` in each measure's "How this is calculated" panel. Keep `display` in plain language and in step with `value`.

## Step 5. The web interface

The overview, measure pages, comparisons, row list and data-quality page work with no interface code. Two optional additions live in `web/src/app.jsx`:

- **Row list columns.** By default the list shows every column of your primary rows. To choose and label them, add an entry to `LISTS` keyed by your dashboard id, following `STEWARDSHIP_LIST`.
- **A detail view** for one row, like respiratory's hourly timeline. Give the `Dashboard` a `detail` builder (`detail=lambda store: MyDetail(store)`, with a `build(row_id)` method that returns JSON-ready data or `None`), and register a React view under your id in `DETAILS`.

Rebuild the interface after any change: `cd web && npm run build`.

## Step 6. Test it

There is no reference implementation to compare a new dashboard against, so the tests must supply one. Follow `tests/test_stewardship.py`:

1. Build a tiny CLIF site in a temporary folder: a handful of hospitalizations written as parquet, each designed to exercise one rule (a dose given twice in a day, a stay crossing midnight, a child who must be excluded, a time whose UTC date differs from its local date).
2. Work out the expected rows by hand and write them into the test, with a comment saying which rule each case checks.
3. Build with `build(load_config(site_yaml), MY_DASHBOARD)` and compare.

Then check plausibility on the synthetic data, and say in your pull request which measures the synthetic data cannot exercise.

Also run:

```bash
uv run pytest                     # everything; needs the synthetic data for most tests
uv run clif-dash build --config config/synth_10k.yaml --dashboard <id>
uv run clif-dash serve --config config/synth_10k.yaml
```

and look at every page of your dashboard in a browser.

## Step 7. Open a pull request

Include:

- the measure definitions and their sources (the decisions file is the place for most of this);
- what the synthetic data can and cannot exercise;
- the CLIF tables and columns the dashboard needs, so other sites can check their data first;
- confirmation that the full test suite passes.

Do not include data, logs or screenshots from a real site.
