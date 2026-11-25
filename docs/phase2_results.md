# Phase 2 results (no LLM)

Run: `uv run python -m evals.phase2` (rules + cache only, no Groq calls), 2026-11-25.
Set: 12 generated synthetic statements (3 layouts, 1–3 months), 731 transactions,
labels from the generator. Output: `data/evals/*_phase2.json`.

| Metric | Result |
|---|---|
| Category accuracy (rules only) | **99.0%** (724/731); every row categorised by a rule, 0 LLM calls |
| Anomaly recall (seeded, detectable) | **100%** (0 missed, all 4 types) |
| Anomaly precision | **98.2%** (1 extra flag) |

- The 7 category misses are "UPI/…/<person>/REFUND SPLIT" credits. The generator labels them
  Transfers In; the rules say Refunds. This is a genuine label ambiguity and the rules were
  left alone.
- Precision was 90.7% on the first run. Seven extra flags came from two rule weaknesses, both
  fixed with tests:
  - a routine SMS charge was counted inside a one-month fee spike;
  - one new ACH merchant was flagged three times.
- The one remaining extra flag is a new ACH broadband mandate in the last month. It is
  arguably correct, but it isn't labelled in the synthetic set.

**Caveat:** the rules and the generator share vocabulary, so 99% is optimistic for real
statements. The rules + LLM comparison, plus a set with rules switched off, is part of the
LLM-inclusive evaluation planned after Phase 4 (`PLAN.md`).

Human review is covered by tests only, as user-led testing is pending:
- payload readability;
- suggestions that reconcile;
- answers typed in rupees;
- pause → suggestion sent back → re-validated → saved;
- batch mode storing the review.

All 182 tests pass offline.
