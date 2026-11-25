# Phase 1a results (2026-11-25)

## Deterministic parser (no LLM) — all 209 digital statements
| Dataset | Stmts | Row recall | Row precision | Rows exact | Date | Debit | Credit | Balance | Description | s/stmt |
|---|---|---|---|---|---|---|---|---|---|---|
| Synthetic (3 layouts) | 9 | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 0.02 |
| AgamiAI Digital Type1 | 100 | 100% | 100% | 99.8% | 100% | 100% | 100% | 100% | 99.82% | 0.06 |
| AgamiAI Digital Type2 | 100 | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 0.06 |

All 29 remaining Type1 errors are descriptions the PDF itself truncates with "..." (unrecoverable).

## Full pipeline (parser + Qwen metadata + repair + validation) — 69 statements
| Dataset | Stmts | Rows exact | Reconciliation pass | Truth's own pass | Opening | Closing | Period | Acct last-4 | Bank |
|---|---|---|---|---|---|---|---|---|---|
| Synthetic | 9 | 100% | 100% | 100% | 100% | 89% | 100% | 100% | 100% |
| AgamiAI Type1 | 30 | 99.8% | 0% | 0% | 100% | 97% | 100% | 100% | 100% |
| AgamiAI Type2 | 30 | 100% | 0% | 0% | 100% | 100% | 100% | 100% | 100% |

All debit/credit/balance fields are 100% correct on all 69 after the guardrail fixes below.
Time per PDF: parser 0.06 s; full pipeline 1–2 s for a single PDF, ~6 s/PDF sustained in a
batch (Groq free tier ≈ 8K tokens/min for Qwen). 0–3 LLM calls per statement (median 1).

- AgamiAI reconciliation is 0% because its ground truth doesn't reconcile either (printed balances
  break in ~12% of rows); those breaks are classified `source_inconsistency` and go to review, not
  LLM retries. Synthetic (exact arithmetic) reconciles 100%.
- Found & fixed during the run: the LLM "repaired" failed (zero-amount) rows by inventing amounts
  (₹2,984.00 + a new balance in t1_00005; ₹45.00 in t2_00010, where "45" came from the time
  "01:45"). Guardrail: a number from the LLM must be printed in that row's amount/balance cells
  (dates, times, refs and descriptions don't count); otherwise the parser's value is kept and the
  row is flagged `llm_unverified` (→ review). Tests cover both cases.
- Closing misses: coral_01 (negative "Dr" closing; prompt fixed after this sample was cached) and
  t1_00030.

## Model bake-off (same sample, Groq free tier)
| Model | Meta opening | Meta closing | LLM-only row recall | Rows exact | s/meta call | s/12-row chunk |
|---|---|---|---|---|---|---|
| **qwen/qwen3.8-27b (default)** | 83%* | 92% | **88.5%** | **50%** | 8.2 | 39.4 |
| openai/gpt-oss-20b | 88%* | 96% | 87.8% | 34% | 3.6 | 8.3 |
| openai/gpt-oss-120b | 88%* | 96% | 40.4% | 16% | 3.7 | 10.7 |

\* all opening misses were the coral layout, whose opening balance is only in the table's "B/F" row
(now read by the parser and authoritative). Qwen is slower only because of the free-tier output
cap (~1000 tok/min). Llama 4 Scout / Llama 3.x are not served on this Groq account.

**Decision:** default model `qwen/qwen3.8-27b`, thinking off. The parser does the rows; the LLM
is used for metadata (1 call/statement) and the rare flagged row.

## Not yet measured
- Pipeline on the remaining 140 digital statements (70 per AgamiAI type; to save quota. Rerun
  with `--mode pipeline`; cached statements are free).
- Scanned PDFs → Phase 1b.
