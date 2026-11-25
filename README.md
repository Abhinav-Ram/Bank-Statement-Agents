# Bank Statement Agents

Upload a bank statement PDF (digital or scanned). A LangGraph pipeline:
1. extracts and validates the transactions, pausing for a human when the numbers don't add up;
2. categorizes them, flags anomalies and writes a summary;
3. stores everything in Postgres.

Next, the same data will answer natural-language questions with cited transactions.

Stack: LangGraph · FastAPI · Postgres/pgvector · React · GroqCloud (Qwen) · Apple Vision OCR

**Status: Phases 0–2 done (ingestion).** Retrieval/Q&A (Phase 3), API (4), LLM-inclusive eval
(4b), UI (5) and demo (6) are next. See `PLAN.md`; conventions are in `CLAUDE.md`.

## What works today

```
PDF ─► parse (text layer, or OCR for scans) ─► statement details (LLM) ─► repair unclear rows (LLM, guarded)
    ─► validate balances ─┬─► retry: re-read scan rows / LLM repair (≤2) ─► validate
                          ├─► human review (pause ⇄ resume, saved in Postgres) ─► validate
                          └─► save ─► categorize ─► flag anomalies ─► summarize
```

| Step | How | Measured |
|---|---|---|
| Digital parsing | Deterministic table reconstruction from the PDF text layer | 100% row recall; dates, amounts and balances 100% (AgamiAI) |
| Scanned statements | Apple Vision OCR, deskew/unshear. If a balance breaks: re-read at 2×, then Tesseract if the two reads disagree | Type1 100% / Type2 99.8% row recall |
| Statement details | Groq/Qwen reads bank, account, period, opening and closing balance | Amounts 100% on 69 statements |
| Validation | Every row's balance and the totals are checked in code. A statement's own misprints are told apart from our reading errors | — |
| Human review | Plain-English cards with ₹ amounts, neighbouring rows, a red-boxed page image and one-click fixes. Corrections are re-checked; a wrong correction is asked again | 6 test cases pass; review testing in `docs/phase2_results.md` |
| Categorization | Merchant extraction → rules → cache (your corrections always win) → LLM only for leftovers | 99.0% (rules only, synthetic) |
| Anomalies | Rules: duplicate charge, large debit, new recurring payment, fee spike. LLM writes one-line explanations, which are number-checked | 100% of seeded anomalies found, 98.2% precision |
| Summary | Totals in code. The LLM writes the wording; any number not in the facts is rejected, falling back to a template | — |

**Guardrails.** The LLM never computes a number. Row repair may only use numbers printed in that
row. OCR retries only choose among readings an engine actually produced. Explanations and
summaries are checked for invented numbers. 189 offline tests cover these.

## Is it agentic yet?

Partly.

**What is agentic today:**
- Ingestion is a **LangGraph state machine**. It decides its own path at runtime: retry, re-read
  with another OCR engine, ask a human, or continue.
- It **loops until the numbers check out**.
- It **pauses mid-run** for human input and resumes from saved state (human in the loop).

**What is not yet:**
- The LLM steps are narrow, guarded tools inside that graph (read the header, repair a row,
  categorize leftovers, word an explanation). They are not free agents choosing their own actions.
- Tool-choosing agents arrive in **Phase 3**: a query graph routes each question to SQL tools or
  vector retrieval and answers with citations.

## Quick start

```bash
cp .env.example .env              # paste your Groq key into LLM_API_KEY
docker compose up -d --build      # db :5433 · api :8080 · web :5173
cd backend && uv sync --all-groups
uv run pytest -q                  # offline, no API calls
```

Data (git-ignored):

```bash
./backend/scripts/download_agami.sh                      # AgamiAI, 1.4 GB
cd backend && uv run python scripts/generate_synthetic.py --scans
```

Try the review step in the terminal. It makes no Groq calls; see `docs/review_testing.md`.

```bash
cd backend
uv run python scripts/make_review_cases.py
uv run python scripts/review.py ../data/review_cases/amount_typo.pdf
uv run python scripts/review.py --dataset agami_scanned_t1 --index 0
```

Evals:
- `uv run python -m evals.extraction --mode parser`
- `uv run python -m evals.phase2`

LLM-inclusive evals are batched into Phase 4b to save the free-tier quota.

## Data

| Set | Used for |
|---|---|
| [AgamiAI/Indian-Bank-Statements](https://huggingface.co/datasets/AgamiAI/Indian-Bank-Statements) (Apache-2.0): 200 statements, digital and scanned, JSON ground truth | Extraction and OCR accuracy |
| Synthetic (our generator) | Exact arithmetic, plus category and anomaly labels |
| `data/review_cases` | Tampered statements for review testing |

All of these are test data. The app itself runs on whatever statement you upload. Never send
real statements to Groq unmasked.

## Docs

| Phase | Spec | Results |
|---|---|---|
| 1a | `docs/phase1a.md` | `docs/phase1a_results.md` |
| 1b | `docs/phase1b.md` | `docs/phase1b_results.md` |
| 2 | `docs/phase2.md` | `docs/phase2_results.md` |
