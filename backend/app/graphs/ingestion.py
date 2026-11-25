"""Graph A: statement ingestion.

    parse → extract_metadata → repair_ambiguous → validate ─┬─ retry (≤2) → validate
                                                             ├─ human_review (interrupt) → validate
                                                             │   (ReviewRequest out, ReviewAnswer in)
                                                             └─ finalize
    finalize → [stages=full] persist → categorize → anomaly → summarize → END

stages="extract" (Phase 1 evals) stops after finalize and writes nothing.
review_mode="interrupt" pauses at human_review until resume_ingestion(); "skip" records
awaiting_review and carries on (batch / evals). Image pages without OCR → needs_ocr.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.extract import extract_metadata, repair_rows
from app.config import get_settings
from app.llm.client import LLMClient
from app.parsing.digital import parse_pdf
from app.schemas.statement import StatementMeta, Txn
from app.services.validate import validate

log = logging.getLogger(__name__)
MAX_RETRIES = 2
CHUNK = 12  # Groq free tier: ~1000 output tokens/min, ~55 tokens per row


class IngestState(TypedDict, total=False):
    pdf: str
    stmt_id: str
    stages: str  # extract | full
    review_mode: str  # interrupt | skip
    reviewed: bool  # a reviewer accepted the statement as-is
    review_log: list[dict]  # each review round: action, how many edits, note
    txn_ids: list[int]
    categories: list[dict]
    anomalies: list[dict]
    summary: dict
    ocr_engine: str | None
    meta_override: dict | None  # evals: known metadata → skip the LLM metadata call
    layouts: list[dict]
    source_type: str  # digital | scanned | mixed
    rows: list[dict]
    meta: dict
    head_text: str
    tail_text: str
    opening_marker: int | None
    report: dict
    attempts: int
    status: str
    llm_calls: int
    tokens: int
    events: list[dict]  # node timings / notes, streamed to the UI in Phase 4


def _llm() -> LLMClient:
    return LLMClient(get_settings())


def _event(state: IngestState, node: str, t0: float, **info: Any) -> list[dict]:
    return [*state.get("events", []), {"node": node, "ms": round((time.perf_counter() - t0) * 1000),
                                       **info}]


def _rows(state: IngestState) -> list[Txn]:
    return [Txn(**r) for r in state["rows"]]


def _dump(rows: list[Txn]) -> list[dict]:
    # mode="json": plain Python types only (OCR coordinates arrive as numpy floats), so the
    # state can be checkpointed to Postgres for a review pause.
    return [r.model_dump(mode="json") for r in rows]


def _plain(obj: Any) -> Any:
    """numpy scalars → Python numbers, recursively (checkpoint-safe)."""
    return json.loads(json.dumps(obj, default=float))


def _usage(llm: LLMClient, state: IngestState) -> dict:
    return {"llm_calls": state.get("llm_calls", 0) + llm.usage["calls"],
            "tokens": state.get("tokens", 0) + llm.usage["prompt_tokens"]
            + llm.usage["completion_tokens"]}


# ---------------------------------------------------------------- nodes
def parse(state: IngestState) -> IngestState:
    t0 = time.perf_counter()
    engine = state.get("ocr_engine", get_settings().ocr_engine)
    res = parse_pdf(state["pdf"], ocr_engine=engine)
    status = "processing" if res.rows and not res.scanned_pages else "needs_ocr"
    return {"rows": _dump(res.rows), "head_text": res.head_text, "tail_text": res.tail_text,
            "ocr_engine": engine, "layouts": _plain(res.layouts), "source_type": res.source_type,
            "opening_marker": res.opening_marker, "status": status, "attempts": 0,
            "llm_calls": 0, "tokens": 0,
            "events": _event(state, "parse", t0, rows=len(res.rows), source=res.source_type,
                             ambiguous=sum(bool(t.ambiguous) for t in res.rows))}


def metadata(state: IngestState) -> IngestState:
    t0 = time.perf_counter()
    llm = _llm()
    if state.get("meta_override"):
        return {"meta": state["meta_override"],
                "events": _event(state, "extract_metadata", t0, source="override")}
    try:
        meta = extract_metadata(llm, state["head_text"], state["tail_text"])
        err = None
    except Exception as e:  # noqa: BLE001 - fall back to what the rows imply
        meta, err = StatementMeta(), f"{type(e).__name__}: {e}"[:200]
    # A printed "B/F" row is read deterministically from the table, which the LLM never sees:
    # it is authoritative for the opening balance.
    if state.get("opening_marker") is not None:
        meta.opening_balance = state["opening_marker"]
    return {"meta": meta.model_dump(), **_usage(llm, state),
            "events": _event(state, "extract_metadata", t0, error=err)}


def repair_ambiguous(state: IngestState) -> IngestState:
    t0 = time.perf_counter()
    rows = _rows(state)
    idx = [i for i, t in enumerate(rows) if t.ambiguous]
    if not idx:
        return {"events": _event(state, "repair_ambiguous", t0, rows=0)}
    llm = _llm()
    rows = _repair(llm, rows, idx, errors=None)
    return {"rows": _dump(rows), **_usage(llm, state),
            "events": _event(state, "repair_ambiguous", t0, rows=len(idx))}


def check(state: IngestState) -> IngestState:
    t0 = time.perf_counter()
    rows = _rows(state)
    rep = validate(StatementMeta(**state["meta"]), rows)
    report = {"ok": rep.ok, "opening_ok": rep.opening_ok, "totals_ok": rep.totals_ok,
              "closing_matches_last": rep.closing_matches_last,
              "breaks": [b.__dict__ for b in rep.breaks], "fixes": rep.fixes,
              "retry_idx": rep.retry_idx, "errors": rep.errors()[:20]}
    return {"rows": _dump(rows), "report": report,
            "events": _event(state, "validate", t0, ok=rep.ok, breaks=len(rep.breaks),
                             fixes=len(rep.fixes))}


def retry(state: IngestState) -> IngestState:
    """Scanned rows: re-read the strip with OCR (no LLM). Everything else: LLM repair with the
    specific validation error."""
    t0 = time.perf_counter()
    rows = _rows(state)
    idx = state["report"]["retry_idx"]
    ocr_idx = [i for i in idx if rows[i].source == "ocr" and not rows[i].ambiguous]
    llm_idx = [i for i in idx if i not in ocr_idx]
    reread = _reread_ocr(state, rows, ocr_idx) if ocr_idx else {}
    llm = _llm()
    if llm_idx:
        errs = [f"row {b['idx']}: balance should be {b['expected'] / 100:.2f} if the amount is "
                f"right, printed {b['printed'] / 100:.2f}"
                for b in state["report"]["breaks"] if b["idx"] in llm_idx]
        rows = _repair(llm, rows, llm_idx, errors=errs)
    return {"rows": _dump(rows), "attempts": state["attempts"] + 1, **_usage(llm, state),
            "events": _event(state, "retry", t0, rows=len(idx), attempt=state["attempts"] + 1,
                             ocr_rows=len(ocr_idx), llm_rows=len(llm_idx), **reread)}


def _reread_ocr(state: IngestState, rows: list[Txn], idx: list[int]) -> dict:
    """Cascade per break: primary engine at 2x; the second engine only if those disagree."""
    from app.ocr.retry import SECOND_UPSCALE, ZOOM, cascade, reread_row

    s = get_settings()
    primary = state.get("ocr_engine") or s.ocr_engine
    secondary = s.ocr_secondary if s.ocr_secondary != primary else None

    def read(row: Txn, step: str) -> Txn | None:
        if step == "zoom":
            return reread_row(state["pdf"], row, state["layouts"], primary, ZOOM)
        if secondary is None:
            return None
        return reread_row(state["pdf"], row, state["layouts"], secondary, SECOND_UPSCALE)

    counts = {"ocr_fixed": 0, "ocr_verified": 0, "ocr_unresolved": 0, "second_engine": 0}
    for i in idx:
        group = [j for j in (i - 1, i) if j >= 0 and rows[j].source in ("ocr", "ocr_reread")]
        if i not in group:
            continue
        prev = (rows[group[0] - 1].balance if group[0] > 0
                else StatementMeta(**state["meta"]).opening_balance)
        outcome, chosen, used_second = cascade(prev, [rows[j] for j in group], read)
        counts[f"ocr_{outcome}"] += 1
        counts["second_engine"] += used_second
        for j, t in zip(group, chosen, strict=True):
            changed = (t.debit, t.credit, t.balance) != (rows[j].debit, rows[j].credit,
                                                         rows[j].balance)
            src = {"fixed": "ocr_fixed" if changed else "ocr_verified",
                   "verified": "ocr_verified", "unresolved": "ocr_reread"}[outcome]
            rows[j] = rows[j].model_copy(update={
                "debit": t.debit, "credit": t.credit, "balance": t.balance, "source": src,
                "notes": [*rows[j].notes, f"reread:{outcome}"]})
    return counts


def _review_request(state: IngestState) -> dict:
    from app.services.review import build_review

    req = build_review(stmt_id=state.get("stmt_id"), pdf=state["pdf"],
                       meta=StatementMeta(**state["meta"]), rows=_rows(state),
                       report=state["report"], layouts=state.get("layouts"),
                       source_type=state.get("source_type", "digital"))
    return req.model_dump(mode="json")


def human_review(state: IngestState) -> IngestState:
    """Pause for a person. The interrupt payload is a ReviewRequest (app/services/review.py):
    plain-English issue cards with ₹ amounts, neighbouring rows, the PDF location and suggested
    fixes. The resume value is a ReviewAnswer ({"action": "accept"} or {"action": "correct",
    "rows": [...], "meta": {...}}); a plain list of paise edits is accepted from code."""
    from langgraph.types import interrupt

    from app.services.review import ReviewAnswer, parse_answer, to_internal

    answer = interrupt(_review_request(state))
    t0 = time.perf_counter()
    ans = parse_answer(answer)
    log_entry: dict = {"round": len(state.get("review_log", [])) + 1}
    if isinstance(ans, ReviewAnswer) and ans.action == "accept":
        log_entry |= {"action": "accept", "note": ans.note}
        return {"reviewed": True, "review_log": [*state.get("review_log", []), log_entry],
                "events": _event(state, "human_review", t0, accepted=True)}
    if isinstance(ans, ReviewAnswer):
        edits, meta_upd = to_internal(ans)
        log_entry |= {"action": "correct", "rows": len(edits), "meta": sorted(meta_upd),
                      "note": ans.note, "keep_remaining": ans.keep_remaining}
    else:
        edits, meta_upd = ans, {}
        log_entry |= {"action": "correct", "rows": len(edits)}
    rows = apply_corrections(_rows(state), edits)
    out: IngestState = {"rows": _dump(rows), "attempts": MAX_RETRIES,  # no more auto retries
                        "review_log": [*state.get("review_log", []), log_entry],
                        "events": _event(state, "human_review", t0, corrected=len(edits),
                                         meta=sorted(meta_upd))}
    if meta_upd:
        out["meta"] = {**state["meta"], **meta_upd}
    if isinstance(ans, ReviewAnswer) and ans.keep_remaining:
        out["reviewed"] = True
    return out


EDITABLE = {"date", "description", "debit", "credit", "balance"}


def apply_corrections(rows: list[Txn], corrections: list[dict]) -> list[Txn]:
    """Each correction: {"idx": i, <field>: value, ...} (money in paise); unknown fields ignored."""
    for c in corrections:
        i = c.get("idx")
        if not isinstance(i, int) or not 0 <= i < len(rows):
            continue
        upd = {k: v for k, v in c.items() if k in EDITABLE}
        rows[i] = rows[i].model_copy(update={**upd, "ambiguous": [], "source": "user",
                                             "notes": [*rows[i].notes, "user_corrected"]})
    return rows


def finalize(state: IngestState) -> IngestState:
    """Final status. The report records the reviewer's decisions, and — for a statement left
    awaiting review (batch mode) — the same ReviewRequest the pause would have shown."""
    if state.get("status") == "needs_ocr":
        return {}
    report = {**state["report"], "review_log": state.get("review_log", [])}
    if state["report"]["ok"] or state.get("reviewed"):
        return {"status": "ok", "report": report}
    if state.get("stages") == "full":
        report["review"] = _review_request(state)
    return {"status": "awaiting_review", "report": report}


# ---------------------------------------------------------------- Phase 2 stages
def persist(state: IngestState) -> IngestState:
    from app.db import connect, migrate
    from app.db.repo import save_statement

    t0 = time.perf_counter()
    with connect() as conn:
        migrate(conn)
        ids = save_statement(conn, state["stmt_id"], state["pdf"], StatementMeta(**state["meta"]),
                             _rows(state), state["status"], state.get("source_type", "digital"),
                             state["report"])
    return {"txn_ids": ids, "events": _event(state, "persist", t0, rows=len(ids))}


def _llm_or_none() -> LLMClient | None:
    return None if get_settings().llm_disabled else _llm()


def categorize_node(state: IngestState) -> IngestState:
    from app.agents.categorize import categorize
    from app.db import connect
    from app.db.repo import cache_lookup, cache_put, set_categories

    t0 = time.perf_counter()
    llm = _llm_or_none()
    with connect() as conn:
        out = categorize(_rows(state), lambda ms: cache_lookup(conn, ms),
                         lambda es: cache_put(conn, es), llm)
        set_categories(conn, [{**a, "id": state["txn_ids"][a["idx"]]} for a in out])
    sources = {}
    for a in out:
        sources[a["source"]] = sources.get(a["source"], 0) + 1
    usage = _usage(llm, state) if llm else {}
    return {"categories": out, **usage,
            "events": _event(state, "categorize", t0, sources=sources)}


def _enriched(state: IngestState) -> list[dict]:
    cats = {a["idx"]: a for a in state.get("categories", [])}
    return [{"idx": i, "date": t.date, "description": t.description, "debit": t.debit,
             "credit": t.credit, "merchant": cats.get(i, {}).get("merchant"),
             "category": cats.get(i, {}).get("category")} for i, t in enumerate(_rows(state))]


def anomaly_node(state: IngestState) -> IngestState:
    from app.agents.explain import explain
    from app.db import connect
    from app.db.repo import earlier_merchants, save_anomalies
    from app.services.anomaly import detect

    t0 = time.perf_counter()
    rows = _enriched(state)
    meta = StatementMeta(**state["meta"])
    first = min((r["date"] for r in rows if r["date"]), default=None)
    llm = _llm_or_none()
    with connect() as conn:
        history = earlier_merchants(conn, meta.account_masked, first, state["stmt_id"])
        found = explain(detect(rows, history), {r["idx"]: r for r in rows}, llm)
        save_anomalies(conn, state["stmt_id"], [
            {**a, "transaction_id": state["txn_ids"][a["idx"]]} for a in found])
    usage = _usage(llm, state) if llm else {}
    return {"anomalies": found, **usage,
            "events": _event(state, "anomaly", t0, found=len(found))}


def summarize_node(state: IngestState) -> IngestState:
    from app.agents.summarize import fact_sheet, narrate
    from app.db import connect
    from app.db.repo import save_summary

    t0 = time.perf_counter()
    rows = _enriched(state)
    by_idx = {r["idx"]: r for r in rows}
    flagged = [{"type": a["type"], "date": by_idx[a["idx"]]["date"],
                "merchant": by_idx[a["idx"]]["merchant"],
                "amount": by_idx[a["idx"]]["debit"] or by_idx[a["idx"]]["credit"]}
               for a in state.get("anomalies", [])]
    fs = fact_sheet(rows, flagged, state["meta"])
    llm = _llm_or_none()
    text, source = narrate(fs, llm)
    period = f"{state['meta'].get('period_start')} to {state['meta'].get('period_end')}"
    with connect() as conn:
        save_summary(conn, state["stmt_id"], period, fs, text, source)
    usage = _usage(llm, state) if llm else {}
    return {"summary": {"fact_sheet": fs, "narrative": text, "source": source}, **usage,
            "events": _event(state, "summarize", t0, source=source)}


def _repair(llm: LLMClient, rows: list[Txn], idx: list[int], errors: list[str] | None) -> list[Txn]:
    for k in range(0, len(idx), CHUNK):
        part = idx[k:k + CHUNK]
        prev = rows[part[0] - 1].balance if part[0] > 0 else None
        try:
            fixed = repair_rows(llm, [rows[i] for i in part], prev, errors)
        except Exception as e:  # noqa: BLE001 - keep parser rows; validation decides what happens
            log.warning("row repair failed for rows %s: %s", part, e)
            fixed = [rows[i] for i in part]
        for i, t in zip(part, fixed, strict=True):
            rows[i] = t
    return rows


# ---------------------------------------------------------------- routing
def after_parse(state: IngestState) -> str:
    return "finalize" if state["status"] == "needs_ocr" else "extract_metadata"


def after_validate(state: IngestState) -> str:
    if state["report"]["retry_idx"] and state["attempts"] < MAX_RETRIES:
        return "retry"
    if (not state["report"]["ok"] and not state.get("reviewed")
            and state.get("stages") == "full" and state.get("review_mode") == "interrupt"):
        return "human_review"
    return "finalize"


def after_finalize(state: IngestState) -> str:
    if state.get("stages") != "full" or state.get("status") == "needs_ocr":
        return END
    return "persist"


def build_graph(checkpointer=None):
    g = StateGraph(IngestState)
    g.add_node("parse", parse)
    g.add_node("extract_metadata", metadata)
    g.add_node("repair_ambiguous", repair_ambiguous)
    g.add_node("validate", check)
    g.add_node("retry", retry)
    g.add_node("human_review", human_review)
    g.add_node("finalize", finalize)
    g.add_node("persist", persist)
    g.add_node("categorize", categorize_node)
    g.add_node("anomaly", anomaly_node)
    g.add_node("summarize", summarize_node)
    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", after_parse, ["extract_metadata", "finalize"])
    g.add_edge("extract_metadata", "repair_ambiguous")
    g.add_edge("repair_ambiguous", "validate")
    g.add_conditional_edges("validate", after_validate, ["retry", "human_review", "finalize"])
    g.add_edge("retry", "validate")
    g.add_edge("human_review", "validate")  # always re-check; a reviewed statement then finalizes
    g.add_conditional_edges("finalize", after_finalize, ["persist", END])
    g.add_edge("persist", "categorize")
    g.add_edge("categorize", "anomaly")
    g.add_edge("anomaly", "summarize")
    g.add_edge("summarize", END)
    return g.compile(checkpointer=checkpointer)


_GRAPH = None


_DEFAULT = object()


def run_ingestion(pdf: str, ocr_engine: str | None | object = _DEFAULT,
                  meta_override: dict | None = None) -> IngestState:
    """Extraction only (Phase 1 evals): no DB writes, no review pause.
    ocr_engine: default = settings.ocr_engine; None = don't OCR (image pages → needs_ocr)."""
    global _GRAPH
    _GRAPH = _GRAPH or build_graph()
    init: IngestState = {"pdf": pdf, "meta_override": meta_override, "stages": "extract"}
    if ocr_engine is not _DEFAULT:
        init["ocr_engine"] = ocr_engine
    out = _GRAPH.invoke(init)
    out.setdefault("report", {"ok": False, "breaks": [], "fixes": []})
    out.setdefault("meta", {})
    return out


# ---------------------------------------------------------------- full ingestion (app)
def start_ingestion(graph, pdf: str, review_mode: str = "interrupt",
                    meta_override: dict | None = None, **extra: Any) -> dict:
    """Full pipeline on a checkpointed graph. Thread id = statement id (from the file hash).
    Returns {"stmt_id", "state", "interrupt"} — interrupt is the review payload if paused."""
    from app.db.repo import statement_id_for

    stmt_id = statement_id_for(pdf)
    cfg = {"configurable": {"thread_id": stmt_id}}
    init: IngestState = {"pdf": pdf, "stmt_id": stmt_id, "stages": "full",
                         "review_mode": review_mode, "meta_override": meta_override, **extra}
    result = graph.invoke(init, cfg)
    if graph.checkpointer is None:  # batch / skip mode: no pause possible, nothing to look up
        return {"stmt_id": stmt_id, "state": result, "interrupt": None}
    return _snapshot(graph, stmt_id)


def resume_ingestion(graph, stmt_id: str, corrections: list[dict] | dict) -> dict:
    """Resume a paused statement with reviewer corrections (or {"accept": True})."""
    from langgraph.types import Command

    graph.invoke(Command(resume=corrections), {"configurable": {"thread_id": stmt_id}})
    return _snapshot(graph, stmt_id)


def _snapshot(graph, stmt_id: str) -> dict:
    snap = graph.get_state({"configurable": {"thread_id": stmt_id}})
    pending = [i.value for t in snap.tasks for i in (t.interrupts or [])]
    return {"stmt_id": stmt_id, "state": snap.values, "interrupt": pending[0] if pending else None}


def postgres_checkpointer():
    """Context manager yielding a PostgresSaver (tables created on first use)."""
    from contextlib import contextmanager

    from langgraph.checkpoint.postgres import PostgresSaver

    @contextmanager
    def _cm():
        with PostgresSaver.from_conn_string(get_settings().database_url) as saver:
            saver.setup()
            yield saver

    return _cm()
