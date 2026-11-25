# Bank Statement Agents

Upload a bank statement PDF (digital or scanned). A LangGraph pipeline:
1. extracts and validates the transactions, pausing for a human when the numbers don't add up;
2. categorizes them, flags anomalies and writes a summary;
3. stores everything in Postgres.

Next, the same data will answer natural-language questions with cited transactions.

Stack: LangGraph · FastAPI · Postgres/pgvector · React · GroqCloud (Qwen) · Apple Vision OCR

**Status: Phases 0–5 done (ingestion, retrieval/Q&A, API, LLM-inclusive eval, React UI).**
Phase 6 (demo polish and the final report) is next. See `PLAN.md`; conventions are in `CLAUDE.md`.

## What works today

```
PDF ─► parse (text layer, or OCR for scans) ─► statement details (LLM) ─► repair unclear rows (LLM, guarded)
    ─► validate balances ─┬─► retry: re-read scan rows / LLM repair (≤2) ─► validate
                          ├─► human review (pause ⇄ resume, saved in Postgres) ─► validate
                          └─► save ─► categorize ─► flag anomalies ─► summarize ─► index (local embeddings → pgvector)

question ─► route (LLM plan / rules) ─► SQL tools (+ guarded free-form SQL) and/or hybrid retrieval ─► grounded answer with cited transactions
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
| Index | Each transaction, month, category, anomaly and summary is embedded **locally** (bge-small) into pgvector, with full-text and trigram indexes | ~200 texts/s on CPU |
| Q&A | A router picks SQL tools, hybrid retrieval (vector + keyword + trigram, RRF) or both. Answers cite transaction ids; numbers must come from the evidence | Golden set: routed 100% vs SQL-only 88% vs RAG-only 64% (offline) |

**Guardrails.** The LLM never computes a number. Row repair may only use numbers printed in that
row. OCR retries only choose among readings an engine actually produced. Explanations and
summaries are checked for invented numbers. 189 offline tests cover these.

## Is it agentic?

Yes, in two places.

**Ingestion** is a LangGraph state machine that decides its own path at runtime:
- retry, re-read with another OCR engine, ask a human, or continue;
- loop until the numbers check out;
- pause for human input and resume from saved state.

**Q&A** (Phase 3) is a tool-using agent:
- **Route:** it reads each question and plans which tools to call, with typed arguments:
  six pre-built SQL tools, a guarded free-form SQL fallback, and hybrid retrieval with filters.
- **Act:** it runs them.
- **Answer:** only from what came back, citing transactions. It remembers earlier turns, so
  follow-ups work.

The LLM never computes numbers: the tools do, and every answer is checked against the evidence.

## Quick start

```bash
cp .env.example .env              # paste your Groq key into LLM_API_KEY
docker compose up -d              # db :5433 · web :5173
cd backend && uv sync --all-groups
uv run uvicorn app.main:app --port 8080   # API, native (Apple Vision OCR); docs at /docs
uv run pytest -q                  # offline, no API calls
```

Open **http://localhost:5173** for the UI: upload or try an example statement, review anything
that doesn't add up, browse the dashboard, and ask questions. Light/dark toggle in the navbar.
The examples come from `uv run python scripts/make_examples.py` (synthetic, in `data/demo/`).

The API runs natively because Apple Vision OCR only works on macOS. Groq is on by default for
uploads and chat, with account-like numbers masked. For real statements, send
`use_cloud_llm=false` and nothing leaves the Mac.

```bash
curl -F file=@statement.pdf localhost:8080/statements           # → statement_id
curl -N localhost:8080/statements/<id>/events                    # live progress (SSE)
curl localhost:8080/statements/<id>/review                       # if paused
curl -X POST localhost:8080/statements/<id>/resume -H 'content-type: application/json' -d '{"action":"accept"}'
curl -N -X POST localhost:8080/chat -H 'content-type: application/json' -d '{"question":"How much did I spend on groceries?"}'
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

Ask questions; this makes no Groq calls by default:

```bash
uv run python scripts/ask.py "How much did I spend on groceries in January?"
uv run python scripts/ask.py          # chat
```

Evals:
- `uv run python -m evals.extraction --mode parser`
- `uv run python -m evals.phase2`
- `uv run python -m evals.qa`

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
| 3 | `docs/phase3.md` | `docs/phase3_results.md` |
| 4 | `docs/phase4.md` | `docs/phase4_results.md` |
| 4b | `PLAN.md` (LLM-inclusive evaluation) | `docs/phase4b_results.md` |
| 5 | `docs/phase5.md` | `docs/phase5_results.md` |
