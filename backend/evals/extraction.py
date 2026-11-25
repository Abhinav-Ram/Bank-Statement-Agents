"""Extraction evals: PDFs in, JSON ground truth to score against.

    uv run python -m evals.extraction --mode parser                       # no LLM, all data
    uv run python -m evals.extraction --mode pipeline --limit 10          # full LangGraph run
    uv run python -m evals.extraction --mode llm_rows --model openai/gpt-oss-20b --chunks 12

Modes
  parser    deterministic parser only (no LLM)
  pipeline  ingestion graph: parser + LLM metadata + LLM repair/retry + validation
  llm_rows  bake-off: the LLM alone extracts 20-row chunks from raw text lines

Results → data/evals/<timestamp>_<mode>_<model>.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime

from app.config import get_settings
from app.datasets.truth import Truth, iter_dataset
from app.llm.client import LLMClient
from app.parsing.digital import parse_pdf
from app.schemas.statement import StatementMeta, Txn
from app.services.validate import validate
from evals.scoring import FIELDS, norm_desc, score_rows

DEFAULT_SETS = "synthetic,agami_digital_t1,agami_digital_t2"


def as_dicts(rows: list[Txn]) -> list[dict]:
    return [{"date": t.date, "description": t.description, "debit": t.debit,
             "credit": t.credit, "balance": t.balance} for t in rows]


def truth_reconciles(tr: Truth) -> bool:
    rows = [Txn(date=r["date"], debit=r["debit"], credit=r["credit"], balance=r["balance"])
            for r in tr.rows]
    return validate(tr.meta, rows).ok


def meta_score(pred: StatementMeta, truth: StatementMeta) -> dict:
    def same(a, b):
        return a is not None and b is not None and (norm_desc(str(a)) == norm_desc(str(b)))

    def contains(a, b):  # "Coral Savings Bank" vs printed "Coral Savings Bank Ltd."
        a, b = norm_desc(str(a or "")), norm_desc(str(b or ""))
        return bool(a and b) and (a in b or b in a)
    return {
        "opening": pred.opening_balance == truth.opening_balance,
        "closing": pred.closing_balance == truth.closing_balance,
        "period": pred.period_start == truth.period_start and pred.period_end == truth.period_end,
        "account_last4": same(pred.account_masked, truth.account_masked),
        "bank": contains(pred.bank_name, truth.bank_name),
    }


OCR_ENGINE: str | None = None
NO_LLM = False


def run_parser(tr: Truth) -> dict:
    t0 = time.perf_counter()
    res = parse_pdf(tr.pdf, ocr_engine=OCR_ENGINE)
    secs = time.perf_counter() - t0
    meta = StatementMeta(opening_balance=res.opening_marker)
    rep = validate(meta, res.rows)
    return {"rows": as_dicts(res.rows), "seconds": secs, "llm_calls": 0, "tokens": 0,
            "reconciled": rep.ok, "status": "ok" if rep.ok else "awaiting_review",
            "ambiguous": sum(bool(t.ambiguous) for t in res.rows),
            "breaks": len(rep.breaks), "fixes": len(rep.fixes), "meta": None}


def run_pipeline(tr: Truth) -> dict:
    from app.graphs.ingestion import run_ingestion

    t0 = time.perf_counter()
    kw = {"meta_override": tr.meta.model_dump()} if NO_LLM else {}
    if OCR_ENGINE:
        kw["ocr_engine"] = OCR_ENGINE
    st = run_ingestion(str(tr.pdf), **kw)
    rows = [Txn(**r) for r in st["rows"]]
    meta = StatementMeta(**st["meta"])
    return {"rows": as_dicts(rows), "seconds": time.perf_counter() - t0,
            "llm_calls": st["llm_calls"], "tokens": st["tokens"],
            "reconciled": st["report"]["ok"], "status": st["status"],
            "ambiguous": sum(bool(t.ambiguous) for t in rows),
            "breaks": len(st["report"]["breaks"]), "fixes": len(st["report"]["fixes"]),
            "meta": meta_score(meta, tr.meta), "attempts": st["attempts"],
            "retry_events": [e for e in st.get("events", []) if e["node"] == "retry"]}


def aggregate(results: list[dict]) -> dict:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by[r["dataset"]].append(r)
    out = {}
    for ds, rs in by.items():
        matched = sum(r["score"]["matched"] for r in rs)
        agg = {
            "statements": len(rs),
            "rows_truth": sum(r["score"]["n_truth"] for r in rs),
            "rows_pred": sum(r["score"]["n_pred"] for r in rs),
            "row_recall": sum(r["score"]["matched"] for r in rs) / max(1, sum(
                r["score"]["n_truth"] for r in rs)),
            "row_precision": matched / max(1, sum(r["score"]["n_pred"] for r in rs)),
            "row_exact": sum(r["score"]["exact_rows"] for r in rs) / max(1, sum(
                r["score"]["n_truth"] for r in rs)),
            "field_acc": {f: sum(r["score"]["field_acc"][f] * r["score"]["matched"] for r in rs)
                          / max(1, matched) for f in FIELDS},
            "statements_all_rows_exact": sum(
                r["score"]["exact_rows"] == r["score"]["n_truth"] == r["score"]["n_pred"]
                for r in rs) / len(rs),
            "reconciliation_pass": sum(r["reconciled"] for r in rs) / len(rs),
            "truth_reconciliation_pass": sum(r["truth_reconciles"] for r in rs) / len(rs),
            "ambiguous_rows": sum(r["ambiguous"] for r in rs),
            "deterministic_fixes": sum(r["fixes"] for r in rs),
            "sec_per_statement": statistics.mean(r["seconds"] for r in rs),
            "llm_calls_per_statement": statistics.mean(r["llm_calls"] for r in rs),
            "tokens_per_statement": statistics.mean(r["tokens"] for r in rs),
        }
        metas = [r["meta"] for r in rs if r.get("meta")]
        if metas:
            agg["meta_acc"] = {k: sum(m[k] for m in metas) / len(metas) for k in metas[0]}
        out[ds] = agg
    return out


def print_table(agg: dict) -> None:
    cols = ["statements", "row_recall", "row_precision", "row_exact", "statements_all_rows_exact",
            "reconciliation_pass", "truth_reconciliation_pass", "ambiguous_rows",
            "sec_per_statement", "llm_calls_per_statement"]
    print(f"{'dataset':20s} " + " ".join(f"{c[:12]:>12s}" for c in cols))
    for ds, a in agg.items():
        vals = [a[c] for c in cols]
        print(f"{ds:20s} " + " ".join(
            f"{v:12.3f}" if isinstance(v, float) else f"{v:12d}" for v in vals))
        print(f"{'':20s} field acc: " + "  ".join(
            f"{f}={a['field_acc'][f]:.4f}" for f in FIELDS)
              + (f"   meta: {a['meta_acc']}" if "meta_acc" in a else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["parser", "pipeline", "llm_rows"], default="parser")
    ap.add_argument("--datasets", default=DEFAULT_SETS)
    ap.add_argument("--limit", type=int, default=None, help="statements per dataset")
    ap.add_argument("--model", default=None, help="override CHAT_MODEL")
    ap.add_argument("--chunks", type=int, default=12, help="llm_rows: chunks per dataset")
    ap.add_argument("--show-errors", type=int, default=0)
    ap.add_argument("--ocr-engine", choices=["vision", "tesseract", "rapidocr"], default=None,
                    help="OCR engine for image pages (scanned datasets)")
    ap.add_argument("--no-llm", action="store_true",
                    help="no Groq calls: ground-truth metadata, LLM repair skipped (saves quota)")
    ap.add_argument("--cached-only", action="store_true",
                    help="no API calls: use cached LLM responses only")
    args = ap.parse_args()

    s = get_settings()
    s.llm_cache_dir = s.data_dir / "cache" / "llm"  # reruns of the same prompts cost no quota
    s.llm_cache_only = args.cached_only
    global OCR_ENGINE, NO_LLM
    OCR_ENGINE = args.ocr_engine
    NO_LLM = s.llm_disabled = args.no_llm
    if args.model:
        s.chat_model = args.model
        if "gpt-oss" in args.model:
            s.llm_extra_body = {"reasoning_effort": "low"}
    if args.mode == "llm_rows":
        from evals.bakeoff import run_llm_rows
        run_llm_rows(args)
        return

    runner = run_parser if args.mode == "parser" else run_pipeline
    results = []
    for ds in args.datasets.split(","):
        for tr in iter_dataset(ds, s.data_dir, args.limit):
            try:
                out = runner(tr)
            except Exception as e:  # noqa: BLE001 - one bad statement must not stop the eval
                print(f"  ! {tr.dataset}/{tr.id}: {type(e).__name__}: {e}")
                continue
            sc = score_rows(tr.rows, out["rows"])
            results.append({"dataset": tr.dataset, "id": tr.id, "score": sc,
                            "truth_reconciles": truth_reconciles(tr),
                            **{k: v for k, v in out.items() if k != "rows"}})
            if args.show_errors and sc["exact_rows"] < sc["n_truth"]:
                _show_errors(tr, out["rows"], args.show_errors)
            if args.mode == "pipeline":
                print(f"  {tr.dataset}/{tr.id}: rows {sc['matched']}/{sc['n_truth']} "
                      f"exact {sc['exact_rows']} status {out['status']} llm {out['llm_calls']} "
                      f"{out['seconds']:.1f}s", flush=True)
    agg = aggregate(results)
    print_table(agg)
    out_dir = s.data_dir / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = s.chat_model.replace("/", "_") if args.mode != "parser" else "noLLM"
    if args.ocr_engine:
        tag += f"_ocr-{args.ocr_engine}"
    if args.no_llm and args.mode == "pipeline":
        tag += "_noLLM-oracleMeta"
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{args.mode}_{tag}.json"
    (out_dir / name).write_text(json.dumps(
        {"mode": args.mode, "model": s.chat_model if args.mode != "parser" else None,
         "ocr_engine": args.ocr_engine,
         "provider": s.llm_provider, "aggregate": agg,
         "llm_network_calls": LLMClient.network_calls, "llm_truncated": LLMClient.truncated,
         "rate_limits": LLMClient.rate_limits, "statements": results}, indent=1))
    print(f"→ data/evals/{name}")


def _show_errors(tr: Truth, pred: list[dict], n: int) -> None:
    from evals.scoring import _eq, align_rows

    pairs = align_rows(tr.rows, pred)
    mi = {i for i, _ in pairs}
    mj = {j for _, j in pairs}
    shown = 0
    print(f"--- {tr.dataset}/{tr.id}")
    for i, j in pairs:
        bad = [f for f in FIELDS if not _eq(f, tr.rows[i], pred[j])]
        if bad and shown < n:
            print(f"   field {bad}: truth={ {f: tr.rows[i][f] for f in bad} } "
                  f"pred={ {f: pred[j][f] for f in bad} }")
            shown += 1
    for i, r in enumerate(tr.rows):
        if i not in mi and shown < n:
            print(f"   missing truth row {i}: {r}")
            shown += 1
    for j, r in enumerate(pred):
        if j not in mj and shown < n:
            print(f"   extra pred row {j}: {r}")
            shown += 1


if __name__ == "__main__":
    main()
