# CLIF ICU Quality Dashboards

ICU quality-improvement dashboards computed from a site's own [CLIF](https://clif-consortium.github.io/website/) database and served inside that site. Eight dashboards ship today:

- **Respiratory failure**: lung-protective ventilation, proning, liberation, sedation and outcomes for adults on invasive mechanical ventilation.
- **Antimicrobial stewardship**: NHSN-style days of therapy per 1,000 days present by ICU, for all antibacterials, the NHSN agent groups and anti-anaerobic agents, and duplicative anaerobic coverage.
- **Microbiology**: blood and urine culture use, blood culture contamination, cultures before new antibiotics, hospital-onset bacteremia and C. difficile, and resistance in S. aureus and gram-negative isolates.
- **Fluids**: input volume per ICU day by CLIF 3.0 input group (IV fluids, crystalloid, colloid, blood products, nutrition, medication fluids, renal replacement fluids, flushes), by ICU and month.
- **ICU flow**: readmission within 48 h, night transfers out, ICU length of stay, time from ED arrival to ICU, and where ICU stays end, from ADT alone.
- **Glycemic control**: ICU days with glucose below 40 and 70 mg/dL or above 300, days with a mean in range, hypoglycemia on insulin infusions, and insulin and glucose-monitoring days.
- **ICU Liberation**: the ABCDEF bundle per ICU day: pain assessment, documented SAT and SBT, sedation assessment and depth, sedative and benzodiazepine infusions, and delirium screening and positivity.
- **Kidney**: acute kidney injury arising in the ICU (KDIGO creatinine criteria), creatinine monitoring, CRRT days and CRRT effluent dose.

Fluids needs CLIF 3.0's `input` table. The others run on CLIF 2.1 or 3.0 tables: 3.0 renamed category values (`IMV` became `imv`, `Assist Control-Volume Control` became `acvc`, `Expired` became `expired`), and the build reads them back as their 2.1 equivalents using clifpy's own crosswalk, so a site gets the same numbers in either version. With no `dashboards:` line in the site config, the build makes every dashboard whose tables the site has and skips the rest.

Everything runs on one machine inside your institution: a batch build reads your CLIF tables and writes the measures, and a local web server shows them. Nothing is sent anywhere. The build is designed for large sites; at UCSF it handles more than 80,000 ICU admissions.

New dashboards are welcome. See [docs/adding-a-dashboard.md](docs/adding-a-dashboard.md).

## Run it at your site

### 1. Install the tools

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python 3.12 and every Python package
- [Node.js](https://nodejs.org/) 18 or later, which builds the web interface
- git

### 2. Get the code and its dependencies

```bash
git clone https://github.com/AartikSarma/clif-dashboards.git
cd clif-dashboards
uv sync
cd web && npm install && npm run build && cd ..
```

### 3. Check your CLIF tables

The build reads parquet files named `<prefix><table>.parquet` (for example `clif_adt.parquet`) from one folder, with datetimes in UTC as CLIF specifies.

| Dashboard | Required tables | Used when present |
| --- | --- | --- |
| Every dashboard | `patient`, `hospitalization`, `adt` | |
| Respiratory failure | `respiratory_support` | `vitals`, `labs`, `position`, `patient_assessments`, `medication_admin_continuous`, `code_status`, `hospital_diagnosis` |
| Antimicrobial stewardship | `medication_admin_intermittent` | |
| Microbiology | `microbiology_culture` | `microbiology_susceptibility`, `microbiology_nonculture`, `medication_admin_intermittent` |
| Fluids | `input` (CLIF 3.0: `recorded_dttm`, `input_category`, `input_group`, `input_volume`) | |
| ICU flow | | |
| Glycemic control | `labs` | `medication_admin_continuous` |
| ICU Liberation | `patient_assessments` | `medication_admin_continuous`, `respiratory_support` |
| Kidney | `labs` | `crrt_therapy`, `vitals` |

A respiratory measure whose table is missing shows as unavailable. A missing required table stops the build with a message that names it.

### 4. Write your site config

Copy the template and edit the copy:

```bash
cp config/site_template.yaml config/my_site.yaml
```

Every `.yaml` file at the top of `config/` except the two templates is ignored by git, so your config, with its local paths, never gets committed. Set at least these keys:

```yaml
clif_dir: /path/to/your/clif/tables        # the folder holding clif_patient.parquet and the rest
file_prefix: clif_                         # the part of each file name before the table name
site_name: My Hospital ICUs                # shown in the dashboard header
site_timezone: America/Chicago             # days and months are grouped in this zone
output_dir: /path/inside/your/institution  # built tables: these contain PHI
dashboards: [respiratory]                  # the dashboards to build and serve: list only those whose required tables you have
workers: 8                                 # parallel processes; up to one fewer than your CPU cores
```

Relative paths are read from the `config/` folder. The time zone matters because CLIF stores UTC and the dashboards count days and months in local time. `start_date` and `end_date` (local dates, `YYYY-MM-DD`) limit the analysis window if you need to.

### 5. Build the measures

```bash
uv run clif-dash build --config config/my_site.yaml
```

This builds every dashboard in your `dashboards:` list. To build only one, add `--dashboard stewardship` (repeat the option for several). The log prints each phase and ends with a `Wrote …` line per dashboard. Each build recomputes everything, so rebuild whenever your CLIF data or a definition changes.

### 6. Open the dashboards

```bash
uv run clif-dash serve --config config/my_site.yaml
```

Open http://127.0.0.1:8000 in a browser on the same machine. With more than one dashboard built, the first page lists them; each dashboard's header links back to that list. The server reads the built tables when it starts, so restart it (Ctrl-C, then the same command) after every build.

Start with each dashboard's **Data quality** page. It shows what your data can and cannot support, so that a gap in documentation is not mistaken for a gap in practice.

### 7. Update to a newer version

```bash
git pull
uv sync
cd web && npm install && npm run build && cd ..
uv run clif-dash build --config config/my_site.yaml
```

Rebuild after every update. If the code defines a measure your last build does not have, the server refuses to start and says to rebuild.

## Keep patient data inside your site

- Everything under `output_dir` contains hospitalization IDs and dates. Keep it on storage your institution approves for PHI.
- The server listens only on the machine it runs on and has no login. Do not expose it on a network.
- `.gitignore` keeps site configs, parquet files, CSV downloads and logs out of git. Check `git status` before any commit all the same.
- When you report a problem, never paste patient data: no rows, IDs, dates, free text or small counts. The [bug report template](.github/ISSUE_TEMPLATE/bug_report.md) says what to include instead.

## Change a definition

Every analytic choice lives in `config/decisions/`: thresholds, time windows, ventilator modes, antimicrobial agent lists, eligibility rules and targets. `platform.yaml` holds the choices every dashboard shares (adult age, encounter linking, outlier handling, time zone), and each dashboard has its own file, such as `respiratory.yaml`. Each measure's page shows these choices in its "How this is calculated" panel, read from the same files, so the page always matches what was computed.

To change a definition for your site:

1. Copy the dashboard's file to the top of `config/`, where git ignores it: `cp config/decisions/respiratory.yaml config/respiratory_my_site.yaml`.
2. Edit the `value` (and the matching `display` text) of the decision you want to change, and note why under `changelog`.
3. Point your site config at the copy:
   ```yaml
   decisions:
     respiratory: respiratory_my_site.yaml
   ```
4. Rebuild.

A dashboard's file cannot change a site-wide value; the build stops and names the decision if it tries. Change those in a copy of `platform.yaml`, named under `decisions:` as `platform`.

## Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| `required CLIF tables not found` or `Required CLIF table not found` | Check `clif_dir` and `file_prefix`; the message names the file it looked for. |
| `No built dashboard found` when serving | Build first, and check that `dashboards:` in your config lists the dashboard. |
| `has no decisions for …: it predates these measures` | The code was updated after your last build. Rebuild. |
| A dashboard is missing from the list | It is not in `dashboards:`, or it was built after the server started. Restart the server. |
| Workers run out of memory | Lower `workers`, or lower `cohort_encounters_per_chunk` (respiratory) or `encounters_per_chunk`. |
| clifpy warns about polars | Run `uv sync` again; the project pins a polars version clifpy supports. |

## Try it without real data

A synthetic CLIF dataset of 10,000 hospitalizations is published by the [synthetic_clif](https://github.com/Common-Longitudinal-ICU-data-Format/synthetic_clif) project. Download it next to this repository:

```bash
gh release download v0.7.0 -R Common-Longitudinal-ICU-data-Format/synthetic_clif -p synth_clif_10k.tar.gz
mkdir -p ../synthetic_clif/synth_clif_10k
tar -xzf synth_clif_10k.tar.gz -C ../synthetic_clif/synth_clif_10k
cp config/synthetic_template.yaml config/synth_10k.yaml
uv run clif-dash build --config config/synth_10k.yaml
uv run clif-dash serve --config config/synth_10k.yaml
```

The template already points at `../synthetic_clif/synth_clif_10k`; edit `clif_dir` in your copy if you put the data elsewhere.

## Tests

With the synthetic data and `config/synth_10k.yaml` in place:

```bash
uv run pytest
```

The respiratory dashboard derives from an earlier single-file dashboard (see Credit), and the tests check it against that dashboard's own output on the v0.7.0 synthetic data: the cohort counts, every episode and proning row, the server's numbers (computed by the original JavaScript under Node), and 26 patient timelines. The other dashboards have no such reference, so each one's tests build a small hand-made CLIF site whose counts were worked out by hand. Tests that need the synthetic data are skipped when it is missing.

## How it works

The build links hospitalizations into encounters across the site, then hands them to each dashboard. Each dashboard copies only the rows it needs from the large tables into chunks with DuckDB, computes its measures chunk by chunk in parallel worker processes, and writes one table of rows (IMV episodes, or ICU stay-months) with a numerator and denominator column per measure. The server keeps those tables in memory and computes each page under the current filters; the browser only draws what it receives.

| Path | Purpose |
| --- | --- |
| `config/site_template.yaml`, `config/synthetic_template.yaml` | Templates for your site config |
| `config/decisions/` | Every analytic choice, per dashboard |
| `src/clif_dash/build.py`, `stage.py`, `config.py` | The build platform: site config, encounter linking, chunked staging |
| `src/clif_dash/dashboard.py` | The contract between the platform and a dashboard |
| `src/clif_dash/dashboards/<id>/` | One folder per dashboard |
| `src/clif_dash/upstream/` | The original respiratory metric logic, changed only where marked `clif_dashboards:` |
| `src/clif_dash/server/` | The API: filters, aggregation, routes |
| `web/` | The React interface |
| `docs/` | How to add a dashboard, and the design notes |

## Known limits

- Only parquet tables are supported.
- A few CLIF 3.0 values have no 2.1 equivalent and are kept as they are. The exception is the respiratory_support device `t_piece`, which is read as IMV with mode Blow by when no mode is charted, as clifpy's 2.1 waterfall records a T-piece trial, so the trial does not end the IMV episode. The respiratory data-quality page counts these rows.
- Each build recomputes every encounter; incremental refresh is not built yet.
- The synthetic data has no documented SAT or SBT assessments, no paralytic or benzodiazepine infusions, no CPOT or BPS pain scores, no point-of-care glucose and none below 40 or above 300 mg/dL, creatinine that varies at random from day to day (so AKI is far too common), one patient who meets the proning criteria, and four antibiotics. Those parts are checked only on paper or by hand-built tests until a site runs them on real data.
- The stewardship measures follow NHSN's counting rules but are not SAARs: there are no NHSN predicted values, so compare within your site, not against national benchmarks.

## License and credit

Copyright (C) 2026 Aartik Sarma and CLIF Consortium contributors.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version (GPL-3.0-or-later). See [LICENSE](LICENSE).

The respiratory measures, definitions and interface derive from [clif-icu-quality-dashboard](https://github.com/cag-lab/clif-icu-quality-dashboard) by Cathy Gao-Howard on behalf of the CLIF Consortium, released under GPL-3.0-or-later. Third-party material keeps its own terms: the proning measures port [CLIF_Proning_Incidence_Severe_ARF](https://github.com/Common-Longitudinal-ICU-data-Format/CLIF_Proning_Incidence_Severe_ARF), and the respiratory-support waterfall and encounter linking come from [clifpy](https://github.com/Common-Longitudinal-ICU-data-Format/clifpy) (Apache-2.0). The stewardship agent groups follow the [NHSN SAAR guide](https://www.cdc.gov/nhsn/pdfs/ps-analysis-resources/aur/saar-2017-2018-508.pdf).
