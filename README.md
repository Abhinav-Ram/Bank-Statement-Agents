# Bank Statement Agents

Bank statement extraction, categorization and hybrid RAG Q&A
(LangGraph · FastAPI · Postgres/pgvector · React · GroqCloud).

Status: **Phase 0** (scaffold). See `PLAN.md` and `CLAUDE.md`.

## Quick start
1. `cp .env.example .env` and paste your Groq key into `LLM_API_KEY` (https://console.groq.com/keys).
2. `docker compose up -d --build`, then open http://localhost:5173
3. `cd backend && uv sync --all-groups && uv run python scripts/check_llm.py`
4. Data: `./scripts/download_agami.sh` (AgamiAI, 1.4 GB) and
   `uv run python scripts/generate_synthetic.py --scans`

Dataset: [AgamiAI/Indian-Bank-Statements](https://huggingface.co/datasets/AgamiAI/Indian-Bank-Statements) (Apache-2.0).
