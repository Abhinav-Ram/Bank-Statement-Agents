# Bank Statement Agents — CLAUDE.md

Bank statement PDF (digital or scanned) → LangGraph pipeline extracts, validates, categorizes,
summarizes, flags anomalies, indexes into pgvector → grounded natural-language Q&A with cited
transactions. Full plan: `PLAN.md`.

## Runtime (read first)
- **Chat LLM: GroqCloud** via its OpenAI-compatible API (`https://api.groq.com/openai/v1`).
  Default model `qwen/qwen3.8-27b` with thinking off (`reasoning_effort: none`). One chat model
  for all agents. (Llama 4 Scout was requested but is not served on this Groq account.)
- Everything about the LLM is in `.env` / `backend/app/config.py` (`LLM_*`, `CHAT_MODEL`,
  `LLM_EXTRA_BODY`). Swapping model or provider (e.g. back to local oMLX) = edit `.env`, no code.
- **Groq has no embeddings API.** Embeddings use a separate `EMBED_*` endpoint (decided in Phase 3).
- **Rate limits (free tier, per model):** 1000 requests/day, 8000 tokens/min, and for Qwen ~1000
  OUTPUT tokens/min where `max_tokens` is counted up front (so `LLM_MAX_TOKENS` < 1000). ≈ one
  20-row extraction chunk per minute. The client retries 429/5xx with Retry-After. Keep LLM calls
  sequential, prompts compact, and cache LLM responses in evals.
- Machine: 8 GB Apple Silicon Mac. OCR runs natively (Apple Vision); Postgres, API, web run in
  docker-compose with small memory caps.
- Do not add Ollama.

## Commands
```bash
docker compose up -d --build                 # db :5433 (host), api :8080, web :5173
curl localhost:8080/health                   # db + llm status
cd backend && uv sync --all-groups           # local dev env
uv run pytest -q                             # tests (offline; no API calls)
uv run python scripts/check_llm.py           # LLM check + latency baseline -> data/baselines/
uv run python scripts/generate_synthetic.py --scans   # synthetic PDFs + ground truth -> data/synthetic/
uv run python -m evals.extraction --mode parser       # parser-only eval, all digital data (~15 s)
uv run python -m evals.extraction --mode pipeline --no-llm --limit 10   # pipeline, NO Groq calls (default for testing)
uv run python -m evals.extraction --mode pipeline --limit 10   # pipeline incl. LLM (uses free-tier quota)
uv run python -m evals.extraction --mode llm_rows --model <id> # model bake-off sample
uv run python -m evals.phase2                         # category accuracy + anomaly P/R (no Groq)
uv run python scripts/make_review_cases.py            # tampered statements + answer key -> data/review_cases/
uv run python scripts/review.py <pdf> | --dataset <name> --index N   # terminal review (no Groq); docs/review_testing.md
# Groq-using evals are batched into Phase 4b (PLAN.md); before that use --no-llm / rules only
uv run python -m evals.extraction --mode parser --ocr-engine vision --datasets synthetic_scan,agami_scanned_t1,agami_scanned_t2 --limit 10
./scripts/download_agami.sh                  # AgamiAI dataset (1.4 GB) -> data/agami/
cd frontend && npm run dev                   # web outside docker (proxies /api -> :8080)
```

## Data
- `data/agami/train/India_Bank_Statement_{Digital,Scanned}_Type{1,2}/NNNNN.{pdf,json}` —
  **primary extraction/OCR eval set**: 400 synthetic Indian *business current-account* statements
  with JSON ground truth (metadata + transactions). Type1 = separate Debit/Credit columns,
  Type2 = one signed Transaction column. Has `failed` transactions/reversals. No category or
  anomaly labels. Apache-2.0, git-ignored.
  - JSON schemas differ: Type1 `debit/credit/balance`; Type2 `cr_dr/transaction_amount/
    available_balance` + `transaction_id`, dd/mm/yyyy dates → normalize in a loader.
  - Scanned_TypeN/NNNNN is an image-only (no text layer) render of Digital_TypeN/NNNNN; the JSON
    is byte-identical. So: 200 unique statements, each in digital + scanned form (~160 rows, ~6 pages).
  - **Ground-truth balances are not arithmetically consistent**: ~12% of rows break the running
    balance (prev + credit − debit ≠ printed balance), every statement has breaks, and only ~8%
    satisfy opening + credits − debits = closing. The PDFs print the same numbers. Therefore on
    AgamiAI, score extraction by **field accuracy vs the JSON**, not by reconciliation; a
    reconciliation failure there is expected and must not trigger retries/review in evals.
    Use `data/synthetic/` (exact arithmetic) to test the validate/retry/review loop.
- `data/synthetic/` — our generator (`backend/app/synthetic/`): personal savings accounts in 3
  layouts with **category labels, seeded anomalies** and simulated scans. Used for
  categorization, anomaly and personal-spend Q&A evals. Deterministic from seed, git-ignored.
- `data/samples/` — REAL statements: git-ignored, never commit. **Real statements must not be sent
  to Groq unmasked** (it is a cloud API): mask account numbers/names before any LLM call, or
  switch `.env` to a local provider for them.
- `data/baselines/` — latency baselines; `data/demo/` — pre-processed demo statements.

## Layout
- `backend/app/config.py` — the ONE config. `backend/app/llm/client.py` — OpenAI-compatible
  client (chat, strict json_schema with json_object fallback, streaming stats, retries, embeddings).
- `backend/app/parsing/` — deterministic parser: `values.py` (amounts → paise, day-first dates,
  Dr/Cr), `layout.py` (header/column/row reconstruction from positioned `Word`s + ruling lines;
  reused for OCR boxes in 1b), `rows.py` (cells → `Txn` + ambiguity flags), `digital.py` (PDF text
  layer, watermark filter, zero-amount rows).
- `backend/app/ocr/` — scanned pages: `preprocess.py` (embedded 300-DPI image, deskew, denoise,
  ruling lines via morphology, coords normalised to A4 points), `engines.py` (Apple Vision default,
  Tesseract, RapidOCR → `Word`s), `retry.py` (re-read row strips with a 2nd engine/upscale, pick
  the reading that closes the balance chain; never computes values). OCR runs natively on macOS
  (`OCR_ENGINE`, `OCR_SECONDARY` in config), not in Docker.
- `backend/app/agents/extract.py` — LLM metadata + row repair. `backend/app/services/validate.py`
  — reconciliation. `backend/app/graphs/ingestion.py` — LangGraph graph A (1a part).
- `backend/app/datasets/truth.py` — ground-truth loaders (AgamiAI T1/T2, synthetic) → one shape.
- `backend/evals/` — `extraction.py` (runner/metrics), `scoring.py` (row alignment), `bakeoff.py`.
  Results in `data/evals/`; LLM responses cached in `data/cache/llm/` (git-ignored).
- `backend/app/db/` — `schema.sql` (idempotent, applied by `migrate()`), `repo.py` (statements,
  transactions, merchant cache — a user correction always wins, anomalies, summaries).
- Phase 2 agents/services: `agents/categorize.py` (merchant normalisation → rules → cache → LLM for
  leftover merchants only, enum + confidence), `services/anomaly.py` (rule-based: duplicate,
  large debit, new recurring, fee spike), `agents/explain.py` (one-line LLM explanations,
  number-checked), `agents/summarize.py` (fact sheet in code, LLM narrative, number check, template
  fallback). Graph: `start_ingestion` / `resume_ingestion` (thread id = statement id),
  `review_mode` interrupt|skip, `stages` extract|full; Postgres checkpointer for real pauses.
  `services/review.py`: ReviewRequest (plain-English issue cards, ₹ text, context rows, PDF
  location, suggested fixes) out / ReviewAnswer (accept | correct, rupees as typed) in — the
  UI contract for the review screen.
- `backend/app/{api,retrieval,tools}/` — filled phase by phase.
- `backend/app/schemas/taxonomy.py` — fixed category taxonomy + anomaly types.
- `db/init.sql` — extensions only; tables live in `backend/app/db/schema.sql`. Local Postgres
  is exposed on host port **5433** (a Homebrew Postgres owns 5432 on this Mac).

## Conventions
- Python 3.11+, uv, ruff (line length 100). Pydantic v2 for every LLM I/O schema.
- Money: integer paise internally; floats only at I/O boundaries.
- LLM calls: temperature 0, strict `json_schema` response_format from the Pydantic model,
  compact output schemas, 20–40 rows per extraction chunk with headers repeated.
- Deterministic code first; the LLM only handles what code can't (metadata, ambiguous rows,
  leftover merchants, narrative wording). Numbers are computed in code, never by the LLM.
- Tests never call the network (use `httpx.MockTransport`).
- LLM row repair must never introduce a number that isn't printed in that row's amount/balance
  cells (`printed_amounts` guardrail). OCR retries likewise only choose among values an engine read.

## Guardrails
- Tests first for guardrails: reconciliation, schema validation, table reconstruction,
  SQL validator, anomaly rules, interrupt/resume.
- Free-form SQL (later): SELECT-only, table allowlist, read-only role, validator.
- `.env` holds API keys: git-ignored; never print or commit keys.

## Working method
One phase at a time: short spec → guardrail tests → implementation → user review.
**Do not start the next phase without the user's approval.**

## Phase status
- [x] Phase 0 — scaffold, compose, LLM check, CLAUDE.md, synthetic generator + AgamiAI data, latency baseline
- [x] 1a digital parse/extract/validate + bake-off (`docs/phase1a.md`, results `docs/phase1a_results.md`) · [x] 1b OCR (`docs/phase1b.md`, results `docs/phase1b_results.md`; bake-off deferred) · [~] 2 code + no-LLM eval done (`docs/phase2.md`, `docs/phase2_results.md`); user-led review testing pending · [ ] 3 · [ ] 4 · [ ] 4b LLM-inclusive eval (one Groq run, see PLAN.md) · [ ] 5 · [ ] 6
