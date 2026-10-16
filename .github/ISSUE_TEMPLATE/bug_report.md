---
name: Bug report
about: A build or dashboard error at your site
---

**Never paste patient data here.** That means no rows, `.head()` output, hospitalization or patient IDs, dates, free text, or counts below 11. A traceback is fine once you have checked it contains none of these.

**What you ran**
<!-- e.g. uv run clif-dash build --config config/<site>.yaml -->

**What happened**
<!-- the traceback or the implausible number, and which page shows it -->

**Your setup**
- Commit (`git rev-parse --short HEAD`):
- Python version (`uv run python --version`):
- How your CLIF tables store datetimes (parquet timestamps or text), if you know:
