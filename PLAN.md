# Project Plan: Bank Statement Agents

Source: `bankstatement_RAG.pdf` (v2). This file records the plan **as adapted** for this machine.

## Deviations from the PDF
| PDF | This repo | Why |
|---|---|---|
| Ollama, native on the Mac | **GroqCloud** (OpenAI-compatible API), default `qwen/qwen3.8-27b` (Llama 4 Scout unavailable) | 8 GB Mac can't hold a local model next to Docker; user choice |
| "Assuming 16 GB → ~8B model" | 8 GB Mac; model size no longer bound by RAM | LLM is remote |
| Ollama `format` for JSON | `response_format: json_schema` (strict), json_object fallback | OpenAI-compatible API |
| `nomic-embed-text` via Ollama | Separate `EMBED_*` endpoint, chosen in Phase 3 | Groq has no embeddings API |
| Synthetic data only + own samples | **AgamiAI Indian-Bank-Statements** (400 digital + scanned, JSON truth) as primary extraction/OCR set; own generator for categories/anomalies | Real-looking scans with ground truth |
| "Local-only processing" | Synthetic/AgamiAI data may go to Groq; **real samples only masked** or via a local provider | Groq is a cloud API |
| API port 8000 | 8080 | 8000 is used by oMLX (still installed as a local fallback) |

Consequences: Groq rate limits (requests/tokens per minute/day) shape evals: sequential calls,
retry on 429, cached results. The Phase 1a bake-off compares Groq models (qwen3.8-27b, gpt-oss-20b,
gpt-oss-120b) instead of local 3–4B models; latency is network + Groq.

## Architecture
React (Vite, TS) → REST/SSE → FastAPI → LangGraph graphs → Postgres + pgvector (docker)
FastAPI → OCR (native: Apple Vision via ocrmac; PaddleOCR / Tesseract compared in 1b)
FastAPI → GroqCloud: one chat model for all agents. Embeddings: separate endpoint (Phase 3).

## Graphs
**A. Ingestion:** detect (per-page text layer) → parse_digital | parse_scanned (300 DPI, deskew,
denoise, OCR word boxes, header-anchored row/column clustering) → extract (LLM: metadata +
ambiguous rows only, 20–40-row chunks) → validate (opening + credits − debits = closing, running
balance; ≤2 targeted retries, re-OCR for scans) → human_review (LangGraph interrupt) → persist →
categorize (rules/cache first, LLM for leftovers in batches, fixed enum + confidence) → anomaly
(rule-based; LLM writes one-line explanations) → summarize (facts in code, LLM narrative,
number-check) → index (chunks → embeddings → pgvector).

**B. Query:** router → pre-built SQL tools (validated SELECT-only fallback) or hybrid retrieval
(vector + full-text/trigram + metadata filters, RRF, light rerank) → grounded answer citing
transaction IDs; chat memory via LangGraph Postgres checkpointer.

## Data model
statements, transactions, categories, merchant_cache, anomalies, chunks (HNSW + tsvector/trigram,
JSONB metadata), summaries, eval_runs. Taxonomy in `backend/app/schemas/taxonomy.py`.

## Phases
| # | Scope | Exit criterion |
|---|---|---|
| 0 | Scaffold, compose, LLM check (Groq), CLAUDE.md, synthetic generator, latency baseline | `docker compose up` works; model responds; sample data + ground truth exist |
| 1a | Digital parse, extract, validate loop, LLM bake-off (2–3 Groq models) on AgamiAI digital | Reconciliation pass rate + row accuracy measured; default model chosen |
| 1b | detect routing, preprocessing, OCR bake-off, table reconstruction, scan retry | Real scans reach a measured reconciliation rate; failures stop at review gate |
| 2 | Persist, interrupt-based review, categorize, anomaly agent, summarize + number-check | Resume-after-correction works; category accuracy + anomaly detection measured |
| 3 | Embeddings, hybrid retrieval, query graph with SQL tools, retrieval-first questions | Golden Q&A passes target; SQL-only vs RAG-only vs hybrid recorded |
| 4 | FastAPI endpoints with SSE, resume endpoint, tracing hooks | E2E API run with streamed progress + pause/resume |
| 4b | **LLM-inclusive evaluation**: one consolidated Groq run over every LLM step (see below) | All LLM metrics recorded in `eval_runs`; quota spent once, re-scoring from cache |
| 5 | React UI: upload, review, table, dashboard, chat, agent timeline, eval page | Full demo flow in browser |
| 6 | Eval report, README, diagram, demo script, pre-processed demo statements | One-command setup, repeatable demo |

## LLM-inclusive evaluation (Phase 4b, after Phase 4)
Until then, evals run without Groq (`--no-llm`, rules only, template fallbacks) to save the
free-tier quota. After Phase 4 every LLM call site exists and runs exactly as the app runs it,
through the API. That makes it the right point to spend the quota once, on the real code paths.
It also comes before the Phase 5 UI, so the eval page has real numbers to show.

| Area | What is measured | Set | ≈ Groq requests |
|---|---|---|---|
| Extraction | metadata accuracy, LLM row repair (guardrail rejections, rows fixed), retry→review rates | AgamiAI digital 40 + scanned 20, synthetic 30 (retry loop) | ~150 |
| Categorization | rules+LLM vs rules-only accuracy; LLM-only accuracy (rules off) on the same labels; cache hit rate | synthetic 12–30 statements | ~20 |
| Anomaly explanations | number-check pass rate, template fallback rate | flagged items from the synthetic set | ~30 |
| Summaries | first-try number-check pass, regeneration rate, fallback rate | 30 statements | ~45 |
| Q&A (Phase 3) | answer accuracy, citation correctness, SQL-only vs RAG-only vs hybrid | golden Q&A set (~50 questions) | ~150 |
| End to end via API | latency per stage (SSE timings), tokens, calls per statement | 5 statements | ~25 |

That is about 400 requests, within one day's 1000. Because of the per-minute token limits it takes
a few hours of wall-clock time, so it runs in the background in a single sitting.

Rules for the run:
- calls are sequential;
- every response is cached in `data/cache/llm`, so re-scoring after a code change uses
  `--cached-only` and costs nothing;
- results go to `eval_runs` and `docs/phase4b_results.md`;
- there is a hard request cap per run.

Before 4b, the only Groq use is a ≤5-call smoke test when a new prompt is added (e.g. the Phase 3
Q&A prompts), to catch schema/prompt breakage early.

## Open items
1. ~~RAM~~ → 8 GB (confirmed).
2. ~~Scanned samples~~ → AgamiAI scanned set (200 statements). Real samples optional, masked before Groq.
3. Scan type: flatbed or phone photos?
4. Languages: English (AgamiAI also has romanized Hindi narrations).
5. Defaults: no auth, local-only demo, synthetic + real samples.
