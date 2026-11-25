"""FastAPI entrypoint.

Run natively (Apple Vision OCR needs macOS):  cd backend && uv run uvicorn app.main:app --port 8080
"""

import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, examples, statements
from app.config import get_settings
from app.llm.client import LLMClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.api.jobs import Runtime, recover
    from app.db import connect, migrate

    with connect() as conn:
        migrate(conn)
    rt = Runtime(inline=getattr(app.state, "inline_jobs", False))
    app.state.rt = rt
    recover(rt)
    yield
    rt.close()


app = FastAPI(title="Bank Statement Agents", version="0.4.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"],
                   allow_headers=["*"])
app.include_router(statements.router, tags=["statements"])
app.include_router(chat.router, tags=["chat"])
app.include_router(examples.router, tags=["examples"])


@app.get("/health", tags=["health"])
def health() -> dict:
    s = get_settings()
    out: dict = {"status": "ok", "llm_provider": s.llm_provider, "chat_model": s.chat_model,
                 "cloud_llm_default": s.cloud_llm_default, "ocr_engine": s.ocr_engine,
                 "embed_model": f"{s.embed_provider}:{s.embed_model}"}

    try:
        with psycopg.connect(s.database_url, connect_timeout=3) as conn:
            exts = [r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()]
        out["db"] = {"ok": True, "extensions": sorted(exts)}
    except Exception as e:  # noqa: BLE001 - health must never raise
        out["db"] = {"ok": False, "error": str(e)[:200]}

    try:
        ids = [m["id"] for m in LLMClient(s).list_models()]
        out["llm"] = {"ok": s.chat_model in ids}
    except Exception as e:  # noqa: BLE001
        out["llm"] = {"ok": False, "error": str(e)[:200]}

    if not (out["db"]["ok"] and out["llm"]["ok"]):
        out["status"] = "degraded"
    return out
