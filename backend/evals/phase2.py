"""Phase 2 evals on the labelled synthetic set: category accuracy + anomaly precision/recall.

    uv run python -m evals.phase2                 # rules + cache only (no Groq) — default
    uv run python -m evals.phase2 --llm           # leftover merchants go to the LLM (uses quota)

Works straight from the generator's ground truth (merchant/category/anomaly labels), so it needs
no database and no PDFs. Results → data/evals/<timestamp>_phase2.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime

from app.agents.categorize import categorize
from app.config import get_settings
from app.llm.client import LLMClient
from app.schemas.statement import Txn
from app.services.anomaly import detect
from app.synthetic.generator import generate_statement
from app.synthetic.render import BANKS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="send leftover merchants to the LLM")
    ap.add_argument("--llm-only", action="store_true",
                    help="rules off: every merchant goes to the LLM (measures the LLM alone)")
    ap.add_argument("--statements", type=int, default=12)
    args = ap.parse_args()
    s = get_settings()
    s.llm_cache_dir = s.data_dir / "cache" / "llm"
    llm = LLMClient(s) if (args.llm or args.llm_only) else None
    cache: dict = {}

    cat_total = cat_ok = 0
    confusion: Counter = Counter()
    sources: Counter = Counter()
    tp = fp = fn = 0
    per_type: Counter = Counter()
    for k in range(args.statements):
        layout = list(BANKS)[k % 3]
        stmt = generate_statement(f"eval_{k}", layout, BANKS[layout], seed=9000 + k,
                                  months=1 + k % 3)
        rows = [Txn(date=t.date, description=t.description, debit=t.debit, credit=t.credit,
                    balance=t.balance) for t in stmt.transactions]
        out = categorize(rows, lambda ms: {m: cache[m] for m in ms if m in cache},
                         lambda es: cache.update({m: (c, conf) for m, c, conf, _ in es}), llm,
                         use_rules=not args.llm_only)
        for a, t in zip(out, stmt.transactions, strict=True):
            cat_total += 1
            sources[a["source"]] += 1
            if a["category"] == t.category:
                cat_ok += 1
            else:
                confusion[(t.category, a["category"])] += 1
        enriched = [{"idx": i, "date": t.date, "description": t.description, "debit": t.debit,
                     "credit": t.credit, "merchant": a["merchant"], "category": a["category"]}
                    for i, (t, a) in enumerate(zip(rows, out, strict=True))]
        found = detect(enriched)
        truth = {(a["type"], i) for a in stmt.anomalies for i in a["txn_idx"]}
        hit_types = set()
        for a in found:
            if (a["type"], a["idx"]) in truth:
                tp += 1
                hit_types.add(a["type"])
            else:
                fp += 1
        for a in stmt.anomalies:
            detectable = not (a["type"] == "new_recurring" and len({t.date[:7] for t in
                                                                    stmt.transactions}) < 2)
            if detectable and a["type"] not in hit_types:
                fn += 1
                per_type[a["type"]] += 1

    res = {
        "llm": "llm_only" if args.llm_only else bool(args.llm), "statements": args.statements,
        "category_accuracy": round(cat_ok / cat_total, 4), "transactions": cat_total,
        "category_sources": dict(sources),
        "top_confusions": [{"truth": a, "pred": b, "n": n} for (a, b), n in
                           confusion.most_common(10)],
        "anomaly": {"precision": round(tp / max(1, tp + fp), 4),
                    "missed_seeded": fn, "missed_by_type": dict(per_type), "false_flags": fp},
        "llm_usage": llm.usage if llm else None,
        "llm_network_calls": LLMClient.network_calls, "llm_truncated": LLMClient.truncated,
        "rate_limits": LLMClient.rate_limits,
    }
    print(json.dumps(res, indent=1))
    tag = "_llm_only" if args.llm_only else "_llm" if args.llm else ""
    out = s.data_dir / "evals" / f"{datetime.now():%Y%m%d_%H%M%S}_phase2{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    print(f"→ data/evals/{out.name}")


if __name__ == "__main__":
    main()
