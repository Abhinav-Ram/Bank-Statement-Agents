# Phase 1a spec: digital parse → extract → validate loop, model bake-off

**Exit criterion:** reconciliation pass rate and row accuracy measured; default model chosen.

## Data (PDF = input, JSON = ground truth)
| Set | Statements | Notes |
|---|---|---|
| AgamiAI Digital Type1 | 100 | Debit / Credit columns, "Rs." amounts, two-line date+time cells, ruled table, diagonal watermark |
| AgamiAI Digital Type2 | 100 | Cr/Dr flag + Transaction Amount, vertically-centred wrapped cells, Transaction ID column |
| Synthetic northwind / coral / summit | 9 | Three more layouts, exact arithmetic, category + anomaly labels |

AgamiAI ground truth balances are themselves inconsistent (~12% of rows), so AgamiAI is scored by
**field accuracy vs JSON**; the reconciliation pass rate is reported next to the ground truth's own
rate (the ceiling). The synthetic set tests the reconciliation / retry loop with exact arithmetic.

## Pipeline (LangGraph, `app/graphs/ingestion.py`)
```
parse_digital → extract_metadata (LLM) → repair_ambiguous (LLM, only flagged rows)
             → validate ──(retryable issues, attempts < 2)──→ retry_chunks (LLM) → validate
                        └→ finalize: status ok | awaiting_review
```
1. **parse_digital** (no LLM, `app/parsing/`): words + ruling lines from PyMuPDF; drop
   non-horizontal text (watermarks); find the header row by vocabulary; columns from vertical
   rules if present, else header clusters + whitespace gaps; rows from horizontal rules if present,
   else nearest date anchor. Each row → typed transaction (integer paise) + `ambiguous` reasons
   (unparseable date/amount, both/neither debit and credit, unknown Dr/Cr). Shared with the
   scanned path in 1b (OCR word boxes are the same `Word` type).
2. **extract_metadata** (LLM, 1 call/statement): bank, holder, account no. (masked in code to
   last 4), period, opening/closing balance from the text above the table + after the last row.
   Strict JSON schema output.
3. **repair_ambiguous** (LLM): flagged rows in chunks of ≤20 with the column headers repeated.
4. **validate** (code): (a) row-derived opening (first balance ∓ first amount) = stated opening;
   (b) running balance per row; (c) opening + credits − debits = closing. Deterministic fix:
   debit/credit swap if it closes a break. A break on a cleanly parsed digital row is recorded as
   `source_inconsistency` (the text layer is exact, the LLM cannot do better) — no LLM retry.
   Breaks on ambiguous / LLM-produced rows are retried with the specific error (max 2).
5. **finalize**: `ok` if all checks pass, else `awaiting_review` (the Phase 2 interrupt hooks in here).

## Metrics (`backend/evals/extraction.py`)
- Row recall / precision (rows aligned to ground truth by date + amount + sign).
- Field accuracy on aligned rows: date, debit, credit, balance, description (whitespace-insensitive).
- Statement exact-match rate (all rows, all fields).
- Reconciliation pass rate (extracted) vs ground-truth pass rate; source inconsistencies flagged.
- Metadata accuracy: opening, closing, period, account last-4.
- LLM calls, tokens, seconds per statement.

## Bake-off
Models available on the Groq account: `qwen/qwen3.8-27b` (default candidate, thinking off),
`openai/gpt-oss-20b`, `openai/gpt-oss-120b` (Llama 4 Scout / Llama 3.x are not served). Each model:
(1) metadata extraction on a fixed sample; (2) **LLM-only row extraction** of 20-row chunks
(raw text lines → rows) to measure raw extraction skill independent of the parser. Free tier
≈ 8K tokens/min per model, so samples are small and LLM responses are cached on disk
(`data/cache/llm/`) to make reruns free.

## Guardrail tests (written first)
Amount/date parsing (Indian + western grouping, Rs./₹, Cr/Dr suffix, signs, `-` blanks),
reconciliation checks and the debit/credit swap fix, LLM schema validation, row alignment
scoring, parser exactness on the synthetic layouts.
