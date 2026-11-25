# Phase 2 spec: persist, human review, categorize, anomalies, summarize

**Exit criterion:** resume-after-correction works; category accuracy measured; anomaly detection
checked on seeded cases. (Testing that needs user input happens when the user asks.)

## Graph A after Phase 2
```
parse → extract_metadata → repair_ambiguous → validate ⇄ retry
   → [not ok] human_review (LangGraph interrupt; Postgres checkpointer) → resume with corrections → validate
   → persist → categorize → anomaly → summarize → END
```
`review_mode`: `interrupt` (app default) pauses at human_review; `skip` (evals/batch) records
`awaiting_review` and continues, so AgamiAI statements (which never reconcile) still flow through.

## Storage (`app/db/schema.sql`, applied idempotently by `app.db.migrate()`)
statements · transactions · categories (seeded from the taxonomy) · merchant_cache ·
anomalies · summaries · eval_runs. `chunks` arrives in Phase 3 with the embedding dimension.
Re-ingesting the same file (sha256) replaces that statement's rows.

## Human review (`app/services/review.py`)
The pause hands the person a **ReviewRequest**: plain JSON (Pydantic models, so the Phase 4 API
exposes them unchanged in OpenAPI and the Phase 5 UI gets typed models). It is written for someone
with the PDF open beside it:

- `headline`, which says how many items are probably misread and how many only need a
  confirmation, and `instructions`, one paragraph on what to do.
- `statement`: file, bank, account, period, opening/closing balance.
- `checks`: four pass/fail/unknown lines (opening, running balance, totals, closing).
- `issues`: one card each, with these fields:
  - `title`, for example "Row 20: the balance doesn't add up".
  - `explanation`, for example "Starting from row 19's balance ₹29,948.69 − ₹1,347.60 debit
    gives ₹28,601.09, but this row's balance reads ₹20,386.59 …", plus why it happened: clean
    text layer, scan confirmed by a re-read, OCR engines disagreed, or repair failed.
  - `severity`: `fix` means probably misread; `check` means the PDF itself likely prints it.
  - `context`: the row before, the flagged row and the row after. Each row has ₹ text plus
    paise, the cells as printed, and how it was read ("read from the scan, confirmed by a
    second read").
  - `location`: page, bbox and page size, for a highlight box on the rendered page.
  - `suggestions`: one-click fixes worked out from the neighbouring balances, likely first.
    Examples are "Amount is ₹500.00 debit (balance is right)", "Balance is ₹850.00 (amount is
    right); this also makes row 3 add up" and "Closing balance is ₹300.00". Nothing is applied
    until the person picks one.
  - `editable`: the fields the person may change.

Amounts use Indian grouping (₹1,23,456.78).

The person sends back a **ReviewAnswer**:
```json
{"action": "accept", "note": "PDF prints these numbers"}
{"action": "correct", "rows": [{"idx": 19, "debit": "9,562.10"}],
 "meta": {"closing_balance": "2,00,000"}, "keep_remaining": false, "note": "..."}
```
Amounts are typed in rupees, as people read them: "1,234.50", 1234.5 or "500 Dr" for an
overdrawn balance. A suggestion's `rows`/`meta` can be sent back unchanged. Corrections are
always re-validated. If something still doesn't add up, the graph pauses again with a fresh
request, unless `keep_remaining` is set. Each round is recorded in `review_log`.

In batch mode (`review_mode="skip"`), the same ReviewRequest is stored in
`statements.reconciliation.review`, so the UI can show it later. Checkpoints live in Postgres
(`langgraph-checkpoint-postgres`); the thread id is the statement id. Code may still resume with
a list of paise edits, or with `{"accept": true}`.

## Categorize (`app/agents/categorize.py`)
1. merchant normalisation from the narration (UPI/NEFT/IMPS/POS/ACH/BILLPAY patterns)
2. rules (keyword → category) and direction (credits only get Income categories)
3. `merchant_cache` (grows from user corrections, `PATCH` later)
4. LLM only for leftover merchants: batches of ≤25 unique merchants, taxonomy as a fixed enum,
   confidence 0–1. Results written back to the cache (source `llm`) so they're asked once.

## Anomalies (`app/services/anomaly.py`, rules) + one LLM call for one-line explanations
- duplicate_charge: same merchant + amount within 1 day
- large_debit: debit ≥ 5× the median of its category (≥3 samples) and ≥ ₹10,000
- new_recurring: Subscriptions/EMI/Insurance/ACH-style debit in the latest month whose merchant
  doesn't appear in earlier months (of this statement or stored history)
- fee_spike: Fees/Charges in a month ≥ 3 entries and ≥ 3× the typical monthly fee total;
  routine fees (recurring amount across months, or SMS/AMC/maintenance-type by name) excluded
- new_recurring flags each new merchant once (its first payment in the month)
Explanations: one batched LLM call; numbers in them must come from the flagged item.

## Summarize (`app/agents/summarize.py`)
Fact sheet in code (income/expense totals, by category, by month + MoM deltas, top merchants,
anomalies). LLM writes ≤120 words from the fact sheet only; a checker confirms every number in
the narrative appears in the fact sheet (formatting-tolerant); one regeneration, then a
deterministic template fallback.

## Groq budget
Per statement: metadata 1 + categorize ⌈leftover merchants / 25⌉ (cache makes reruns ~0) +
anomaly explanations 1 (only if any) + summary 1–2. Offline tests use fakes.
