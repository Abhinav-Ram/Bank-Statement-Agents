"""FastAPI entrypoint. Phase 0: health only."""

import psycopg
from fastapi import FastAPI

from app.config import get_settings
from app.llm.client import LLMClient

app = FastAPI(title="Bank Statement Agents", version="0.0.1")


@app.get("/health")
def health() -> dict:
    s = get_settings()
    out: dict = {"status": "ok", "llm_provider": s.llm_provider, "chat_model": s.chat_model}

    try:
        with psycopg.connect(s.database_url, connect_timeout=3) as conn:
            exts = [r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()]
        out["db"] = {"ok": True, "extensions": sorted(exts)}
    except Exception as e:  # noqa: BLE001 - health must never raise
        out["db"] = {"ok": False, "error": str(e)[:200]}

    try:
        ids = [m["id"] for m in LLMClient(s).list_models()]
        out["llm"] = {"ok": s.chat_model in ids, "models": ids}
    except Exception as e:  # noqa: BLE001
        out["llm"] = {"ok": False, "error": str(e)[:200]}

    if not (out["db"]["ok"] and out["llm"]["ok"]):
        out["status"] = "degraded"
    return out
