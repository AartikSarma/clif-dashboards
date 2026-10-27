# Microbiology dashboard: measures built and proposed

Status: nine measures built (M1 to M9); further measures proposed below. Definitions and their sources are in `config/decisions/microbiology.yaml`; tests are in `tests/test_microbiology.py`.

## Built

| # | Measure | Definition checked against | Synthetic data |
| --- | --- | --- | --- |
| M1 | Blood culture draws per 1,000 days present | Local choice (CLIF has no specimen number, so a draw is one collection time) | Exercised |
| M2 | Urine culture draws per 1,000 days present | Local choice | Exercised |
| M3 | Blood culture contamination | NHSN BSI chapter (single common commensal; matching commensals on the same or next calendar day); NHSN common commensal list (2024) mapped to CLIF | Reads 0%: the synthetic data has no common commensals |
| M4 | Blood culture before a new IV antibacterial start | Local choice (48 h gap, 48 h window) | Reads 0.5%: the synthetic generator draws blood cultures after the first dose |
| M5 | Hospital-onset bacteremia and fungemia per 1,000 days present | Components from NHSN (organisms from the BSI chapter, hospital day 4 from the LabID chapter, 14-day repeat window); NHSN has no HOB protocol, and studies use day 3 or 4 | Exercised |
| M6 | Incident hospital-onset C. difficile per 10,000 days present | NHSN MDRO and CDI Module (LabID: 14-day duplicates, hospital day 4, incident after 56 days, per 10,000 patient days) | Reads near 0: synthetic C. difficile tests fall on hospital day 1 or 2 |
| M7 | Methicillin resistance among S. aureus | Local choice (first isolate per encounter, after CLSI M39) | Exercised |
| M8 | Ceftriaxone non-susceptibility among Enterobacterales | Local choice | Exercised |
| M9 | Carbapenem non-susceptibility among Enterobacterales, P. aeruginosa and A. baumannii | Local choice | Exercised |

The hand-made test site exercises every rule the synthetic data cannot: contaminants and matched commensals, the repeat window, starts after ward doses and oral doses, duplicate and recurrent C. difficile, first isolates, and ignored ertapenem results.

## Proposed, not built

- **Antibiotics continued after negative cultures.** Share of new starts still running 72 h later when every blood culture drawn around the start stayed negative: the stewardship "antibiotic time-out". Needs a rule for when a course ends.
- **Time to an active antibiotic for bacteremia.** Hours from the first positive blood culture to the first dose of an agent the isolate is susceptible to. Needs an agent-to-susceptibility map.
- **Urine cultures without pyuria.** Share of urine cultures sent without urinalysis evidence of pyuria (CAUTI diagnostic stewardship). Needs the urinalysis white-cell result in `labs`.
- **Respiratory culture use and positivity** in ventilated patients, linked to the respiratory dashboard.
- **Vancomycin-resistant enterococci** among Enterococcus isolates, alongside M7 to M9.

Not computable from CLIF 2.1: solitary blood culture sets (no specimen or set number), and CLABSI or CAUTI rates (no line or catheter days).
