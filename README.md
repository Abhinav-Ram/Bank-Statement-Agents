# Bank Statement Agents

Upload a bank statement PDF, digital or scanned, and ask questions about it in plain English. The
app reads the statement, checks that every balance adds up, asks you when something doesn't,
categorises the spending, flags anything unusual, and answers questions with the exact
transactions it used.

Stack: LangGraph, FastAPI, Postgres with pgvector, React with DaisyUI, Groq (Qwen 3) for the LLM,
Apple Vision for OCR, and a local embedding model.

## Why a general RAG setup isn't enough here

Retrieval-augmented generation is now the usual way companies give an LLM access to their own
data. In a 2026 VentureBeat survey, retrieval over documents or a vector index was the most common
way teams supplied context to AI, and the same survey found that trust in the answers is lagging
behind ([VentureBeat](https://venturebeat.com/ai/the-ai-context-gap-enterprise-ai-organizations-have-a-trust-problem-not-a-retrieval-problem-and-most-are-still-building-the-fix)).
Teams are now fixing the retrieval layers they already have, and interest in hybrid retrieval
has risen sharply ([VentureBeat](https://venturebeat.com/ai/the-retrieval-rebuild-why-hybrid-retrieval-intent-tripled-as-enterprise-rag-programs-hit-the-scale-wall)).

Financial documents show why. Chunking a table into text for embedding breaks its structure and
weakens multi-step and aggregate reasoning
([TableRAG](https://arxiv.org/html/2506.10380v1)), and a benchmark of 32,908 questions over
financial text and tables remained hard for current models and RAG methods
([T2-RAGBench](https://arxiv.org/html/2506.12071v1)). A bank statement is mostly table, and the
questions people ask about one are mostly arithmetic: how much, since when, compared with what.

So this project treats the statement as data with rules. Money is stored as integer paise, every
row has to satisfy *previous balance + credit − debit = balance*, and totals are computed by SQL.
The LLM reads headers, repairs unclear rows from the numbers printed on them, chooses tools, and
writes wording. It never produces a number that the code or the PDF didn't. Semantic search is
still there for the questions arithmetic can't answer, like "that transfer to the plumber" or a
merchant printed as `AMZN MKTP IN*2K4`.

## Setup

You need a Mac (tested on Apple Silicon with 8 GB of RAM), [Docker Desktop](https://www.docker.com/products/docker-desktop/)
and [uv](https://docs.astral.sh/uv/). A [Groq API key](https://console.groq.com/keys) is optional;
without one the app runs on rules and templates.

```bash
./start.sh
```

The script checks the tools, creates `.env` from `.env.example` if it's missing, starts Postgres
and the web UI in Docker, installs the backend, downloads the embedding model (about 65 MB, once),
makes the example statements and starts the API. It is safe to run again. Then open
http://localhost:5173. API docs are at http://localhost:8080/docs.

```bash
./stop.sh        # stops the API and the containers; data is kept
```

The API runs on the Mac itself, not in Docker, because Apple Vision OCR only works on macOS.

## Demo (about five minutes)

1. On the Upload page, try the example "Statement with a typo". It pauses for review in under a
   second.
2. Click Review now. The card shows a crop of the PDF page with the problem row boxed in red, the
   rows around it, and the likely fix ("Amount is ₹842.60 debit"). Pick it, keep the follow-on
   totals card as printed, and click Apply & re-check. The pipeline re-checks your answer and
   carries on, live.
3. Open the Overview tab for the totals, category and monthly charts, the summary and the flagged
   anomalies. In Transactions, change a category; it applies to every row of that merchant and is
   remembered for future statements.
4. On the Ask page, try "How much did I spend on groceries in January?" (answered by a SQL tool)
   and "what did I pay the plumber?" (answered by search). Each answer shows which path answered
   it and cites the transactions.
5. Try the scanned example to watch OCR, then look at the Agent timeline tab and the Evals page.

The cloud LLM switch on the Upload and Ask pages decides whether statement text goes to Groq.
Account, card and phone numbers are masked before any call, but payee names are not, so keep it
off for your own statements. Everything then stays on the Mac.

## How it works

Ingestion is one LangGraph graph:

```
PDF ─► parse ─► read details ─► repair unclear rows ─► check balances
                                                            │
       ┌─────────────── retry: re-read or repair ◄──────────┤ our reading looks wrong
       │                human review (pause, resume) ◄──────┤ needs a person
       ▼                                                    │
     check again                                    save ◄──┘ adds up
                                                     │
                       categorise ─► flag anomalies ─► summarise ─► index for search
```

Questions go through a second graph:

```
question ─► route ─► act: SQL tools and/or hybrid search ─► answer from the evidence, with citations
```

What each step does:

- Parse uses the PDF's text layer, or Apple Vision OCR for scans. Read details asks the LLM for
  the bank, account, period and balances; printed B/F and C/F rows are read in code and win.
- Repair lets the LLM fix an unclear row, but only with numbers printed in that row. A retry
  re-reads a scanned row at 2× and asks Tesseract only if the two Vision reads disagree.
- Categorise tries rules, then a merchant cache, then the LLM for merchants nothing else knows.
  Anomalies are rules: duplicate charge, unusually large debit, new recurring payment, fee spike.
- Summaries and anomaly explanations are worded by the LLM from totals computed in code, and any
  number not in those totals sends the text back to a template.
- Routing picks from six SQL tools (plus a validated, read-only free-form SQL fallback) and a
  hybrid search that merges vector, keyword and trigram results with reciprocal rank fusion.

Some details that matter for bank data:

- The parser rebuilds the table from word positions and ruling lines, and the same code reads
  OCR boxes from scans. Scans are deskewed and unsheared before OCR.
- A row that doesn't add up is told apart from a typo printed by the bank itself. Only our own
  reading errors are retried; the rest go to review.
- The same statement uploaded twice, a scan of a statement already saved, or overlapping periods
  never double count. Rows are matched per account on date, amount and running balance.
- The review screen is driven by a typed contract (`ReviewRequest` out, `ReviewAnswer` in), so the
  UI and the terminal tool show the same cards. A wrong correction is asked again; a statement
  that prints inconsistent numbers can be kept as printed.
- Free-form SQL runs as a read-only database role, in a read-only transaction, with a time limit,
  after a parser-based validator.

## Results

All numbers come from the eval runs in `docs/`. The data is synthetic (AgamiAI and our own
generator), so the results show the system works on known data. Accuracy on real statements is
still untested.

| What | Result |
|---|---|
| Digital statements, parser only (AgamiAI) | 100% of rows found; dates, amounts and balances 100% |
| Scanned statements, Apple Vision + re-read (AgamiAI) | Type 1: 100% of rows, 100% of balances. Type 2: 99.8% of rows, 99.9% of balances |
| Statement details read by Groq | 100% on 16 AgamiAI statements (10 digital, 6 scanned) and 3 synthetic |
| Categories, rules + cache + LLM | 99 to 100% on synthetic statements |
| Categories, LLM alone with rules switched off | 96.5% |
| Anomalies (seeded) | all found, 98.2% precision |
| Q&A, offline: SQL only / search only / routed | 88% / 64% / 100% of 25 golden questions |
| Q&A with Groq planning and answering | 100% on golden and held-out questions after fixes (84% before) |

The Q&A comparison is the main argument for the design: SQL alone was exact on totals and missed
fuzzy questions, search alone did the opposite, and routing between them got both.

Several fixes were made after looking at the golden-set failures, so the held-out questions are
the fairer measure. The whole Groq evaluation used 90 calls and took 12 minutes on the free tier. There are 260 tests,
and they run offline.

## Developing

```bash
cd backend && uv run pytest -q                      # tests (offline, no API calls)
uv run python -m evals.qa                           # golden Q&A, SQL vs search vs routed
uv run python -m evals.extraction --mode parser     # extraction accuracy (needs the AgamiAI data)
uv run python -m evals.suite_4b --smoke             # a few Groq calls across the LLM evals
uv run python scripts/ask.py "your question"        # Q&A in the terminal
uv run python scripts/review.py <pdf>               # review in the terminal
./scripts/download_agami.sh                         # AgamiAI dataset, 1.4 GB, into data/agami/
cd ../frontend && npm run dev                       # UI outside Docker
npm run gen:api                                     # regenerate API types from the running API
```

Project conventions and the full command list are in `CLAUDE.md`; the original plan and how it
was adapted are in `PLAN.md`.

## Data

| Set | Used for |
|---|---|
| [AgamiAI/Indian-Bank-Statements](https://huggingface.co/datasets/AgamiAI/Indian-Bank-Statements) (Apache-2.0): 200 statements, each digital and scanned, with JSON ground truth | extraction and OCR accuracy |
| Our generator: personal accounts in three layouts, with category labels and planted anomalies | arithmetic checks, categories, anomalies, Q&A |
| `data/demo/`: four synthetic example statements | the one-click examples in the UI |

Real statements belong in `data/samples/`, which is git-ignored.

## Docs

| Phase | Spec | Results |
|---|---|---|
| 1a digital parsing | `docs/phase1a.md` | `docs/phase1a_results.md` |
| 1b OCR | `docs/phase1b.md` | `docs/phase1b_results.md` |
| 2 review, categories, anomalies, summary | `docs/phase2.md` | `docs/phase2_results.md` |
| 3 retrieval and Q&A | `docs/phase3.md` | `docs/phase3_results.md` |
| 4 API | `docs/phase4.md` | `docs/phase4_results.md` |
| 4b evaluation with the LLM | `PLAN.md` | `docs/phase4b_results.md` |
| 5 UI | `docs/phase5.md` | `docs/phase5_results.md` |
