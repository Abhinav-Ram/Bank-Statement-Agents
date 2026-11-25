"""LLM bake-off on a fixed sample (same for every model; responses cached on disk).

  1. metadata  : extract_metadata on N statements per dataset, scored vs ground truth
  2. llm_rows  : the LLM alone turns raw printed text lines (12 rows + the header line) into rows,
                 scored vs ground truth — raw extraction skill with no parser help

    uv run python -m evals.extraction --mode llm_rows --model qwen/qwen3.8-27b
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime

import pymupdf

from app.agents.extract import extract_metadata
from app.config import get_settings
from app.datasets.truth import iter_dataset
from app.llm.client import LLMClient
from app.parsing.digital import page_words, parse_pdf
from app.parsing.layout import find_header, group_lines
from app.parsing.values import iso, parse_date
from app.schemas.statement import LLMRows
from evals.extraction import meta_score
from evals.scoring import FIELDS, score_rows

ROWS_PER_CHUNK = 12
PROMPT = ("Extract every transaction row from this bank statement excerpt. The first line is "
          "the table header. Dates as YYYY-MM-DD (source dates are day-first). Amounts as plain "
          "numbers without commas or currency; null for an empty debit/credit. A cell may wrap "
          "onto the next line.\n\n")


def _chunk_text(pdf, rows, start: int) -> str | None:
    part = rows[start:start + ROWS_PER_CHUNK]
    if len({r.page for r in part}) != 1:
        return None
    page = pymupdf.open(pdf)[part[0].page - 1]
    words = page_words(page)
    hdr = find_header(words)
    top, bot = part[0].bbox[1] - 1, part[-1].bbox[3] + 1
    lines = group_lines([w for w in words if top <= w.cy <= bot])
    head = " | ".join(" ".join(w.text for w in ln) for ln in group_lines(hdr)) if hdr else ""
    return head + "\n" + "\n".join(" ".join(w.text for w in ln) for ln in lines)


def run_llm_rows(args) -> None:
    s = get_settings()
    s.llm_cache_dir = s.data_dir / "cache" / "llm"
    llm = LLMClient(s)
    rng = random.Random(42)
    out = {"model": s.chat_model, "extra_body": s.llm_extra_body, "metadata": [], "rows": []}
    n_meta = args.limit or 8
    t_start = time.perf_counter()
    for ds in args.datasets.split(","):
        truths = iter_dataset(ds, s.data_dir)
        sample = rng.sample(truths, min(n_meta, len(truths)))
        for tr in sample:  # 1. metadata
            res = parse_pdf(tr.pdf)
            t0 = time.perf_counter()
            try:
                m = extract_metadata(llm, res.head_text, res.tail_text)
                sc, err = meta_score(m, tr.meta), None
            except Exception as e:  # noqa: BLE001
                sc, err = {k: False for k in ("opening", "closing", "period", "account_last4",
                                              "bank")}, str(e)[:150]
            out["metadata"].append({"dataset": ds, "id": tr.id, "score": sc, "error": err,
                                    "seconds": time.perf_counter() - t0})
            print(f"  meta {ds}/{tr.id}: {sc} {err or ''}", flush=True)
        # 2. LLM-only row chunks
        chunks = 0
        for tr in sample:
            if chunks >= args.chunks // 3 + 1:
                break
            res = parse_pdf(tr.pdf)
            if len(res.rows) != len(tr.rows):
                continue
            start = rng.randrange(0, max(1, len(res.rows) - ROWS_PER_CHUNK))
            text = _chunk_text(tr.pdf, res.rows, start)
            if not text:
                continue
            truth_rows = tr.rows[start:start + ROWS_PER_CHUNK]
            t0 = time.perf_counter()
            try:
                got = llm.chat_json([{"role": "user", "content": PROMPT + text}], LLMRows).rows
                pred = [{"date": iso(parse_date(r.date)), "description": r.description,
                         "debit": round((r.debit or 0) * 100), "credit": round((r.credit or 0) * 100),
                         "balance": None if r.balance is None else round(r.balance * 100)}
                        for r in got]
                err = None
            except Exception as e:  # noqa: BLE001
                pred, err = [], str(e)[:150]
            sc = score_rows(truth_rows, pred)
            out["rows"].append({"dataset": ds, "id": tr.id, "start": start, "score": sc,
                                "error": err, "seconds": time.perf_counter() - t0})
            print(f"  rows {ds}/{tr.id}@{start}: matched {sc['matched']}/{sc['n_truth']} "
                  f"exact {sc['exact_rows']} {err or ''}", flush=True)
            chunks += 1
    out["usage"] = llm.usage
    out["wall_seconds"] = time.perf_counter() - t_start
    out["summary"] = summarize(out)
    print(json.dumps(out["summary"], indent=1))
    d = s.data_dir / "evals"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d_%H%M%S}_bakeoff_{s.chat_model.replace('/', '_')}.json"
    (d / name).write_text(json.dumps(out, indent=1))
    print(f"→ data/evals/{name}")


def summarize(out: dict) -> dict:
    meta = out["metadata"]
    rows = out["rows"]
    n_truth = sum(r["score"]["n_truth"] for r in rows)
    matched = sum(r["score"]["matched"] for r in rows)
    summary = {
        "model": out["model"],
        "meta_n": len(meta),
        "meta_acc": {k: sum(m["score"][k] for m in meta) / max(1, len(meta))
                     for k in ("opening", "closing", "period", "account_last4", "bank")},
        "meta_errors": sum(bool(m["error"]) for m in meta),
        "row_chunks": len(rows),
        "row_recall": matched / max(1, n_truth),
        "row_exact": sum(r["score"]["exact_rows"] for r in rows) / max(1, n_truth),
        "row_field_acc": {f: sum(r["score"]["field_acc"][f] * r["score"]["matched"] for r in rows)
                          / max(1, matched) for f in FIELDS},
        "row_errors": sum(bool(r["error"]) for r in rows),
        "sec_per_meta_call": sum(m["seconds"] for m in meta) / max(1, len(meta)),
        "sec_per_row_chunk": sum(r["seconds"] for r in rows) / max(1, len(rows)),
        "tokens": out["usage"]["prompt_tokens"] + out["usage"]["completion_tokens"],
    }
    return summary

