"""Metric logic from cag-lab/clif-icu-quality-dashboard (commit b3cdf29), GPL-3.0-or-later.

Copied from upstream pipeline/ with three changes, each marked "clif_dashboards":
apply_outliers takes the whole-table FiO2-percent flag; metrics.compute can skip the
mortality model (fit_mortality_model runs once over the whole cohort); and
proning.compute can skip expected proning (expected_proning runs once). Keep the
rest identical to upstream so tests/test_oracle.py stays meaningful.
"""
