"""Phase 0 LLM check + latency baseline against the configured provider (GroqCloud by default).

    uv run python scripts/check_llm.py              # check + 3-run baseline
    uv run python scripts/check_llm.py --runs 5

Checks: server reachable, configured chat model served, plain reply, schema-constrained
JSON (a real extraction prompt), embeddings (if EMBED_MODEL set). Writes
data/baselines/latency_<model>_<date>.json with TTFT, decode tok/s and an extraction
chunk timing, so later phases can compare against it.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel

from app.config import get_settings
from app.llm.client import LLMClient, LLMError


class Row(BaseModel):
    date: str
    description: str
    debit: float | None
    credit: float | None
    balance: float


class Rows(BaseModel):
    rows: list[Row]


EXTRACT_PROMPT = """Extract every transaction row from this bank statement excerpt.
Dates as YYYY-MM-DD. Amounts as plain numbers (no commas). Use null for an empty debit/credit.

Date     Narration                                   Withdrawal   Deposit    Balance
01/01/25 NEFT CR-ACME SOFTWARE PVT LTD-SALARY JAN                 95,000.00  1,14,454.54
03/01/25 IMPS/P2A/838560184785/PRESTIGE RENTALS/RENT  22,000.00              92,454.54
03/01/25 BILLPAY/AIRTEL POSTPAID/715652060496            513.08              91,941.46
04/01/25 POS Z497 APOLLO PHARMACY 0Z497                  414.42              91,527.04
04/01/25 POS AB7M MYNTRA DESIGNS PVT                     892.98              90,634.06
05/01/25 ACH D- HDB FINANCIAL SERV-418464905370       12,999.00              77,635.06
"""
EXPECTED_BAL = [114454.54, 92454.54, 91941.46, 91527.04, 90634.06, 77635.06]


def ok(msg):
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg):
    print(f"  \033[31m✗\033[0m {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--no-baseline", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    llm = LLMClient(s)
    print(f"{s.llm_provider} @ {s.llm_base_url}  chat_model={s.chat_model}  "
          f"embed_model={s.embed_model or '-'}")
    if not s.llm_api_key or s.llm_api_key.startswith("paste-"):
        fail("LLM_API_KEY is not set: paste your key into .env (https://console.groq.com/keys)")
        return 1

    # 1. reachable + model listed
    try:
        ids = [m["id"] for m in llm.list_models()]
    except Exception as e:  # noqa: BLE001
        fail(f"endpoint not reachable / auth failed: {e}")
        print("    -> check LLM_BASE_URL and LLM_API_KEY in .env")
        return 1
    ok(f"endpoint reachable, {len(ids)} models")
    if s.chat_model not in ids:
        fail(f"CHAT_MODEL '{s.chat_model}' not available. Models: {', '.join(sorted(ids))}")
        return 1

    # 2. plain reply (also loads the model; cold start timed)
    try:
        t0 = time.perf_counter()
        # generous max_tokens: reasoning models spend tokens before the visible answer
        reply = llm.chat([{"role": "user", "content": "Reply with exactly: READY"}], max_tokens=300)
        cold = time.perf_counter() - t0
    except LLMError as e:
        fail(f"chat failed: {e}")
        return 1
    ok(f"chat reply {reply.strip()!r} ({cold:.2f}s)")

    # 3. schema-constrained extraction
    t0 = time.perf_counter()
    try:
        rows = llm.chat_json([{"role": "user", "content": EXTRACT_PROMPT}], Rows).rows
        extract_s = time.perf_counter() - t0
        bal_hits = sum(abs(r.balance - e) < 0.01 for r, e in zip(rows, EXPECTED_BAL))
        mode = "json_schema" if llm.json_schema_supported else "json_object fallback"
        (ok if bal_hits == len(EXPECTED_BAL) and len(rows) == 6 else fail)(
            f"JSON extraction ({mode}): {len(rows)}/6 rows, {bal_hits}/6 balances exact, "
            f"{extract_s:.1f}s"
        )
    except Exception as e:  # noqa: BLE001
        extract_s, bal_hits, rows = None, 0, []
        fail(f"JSON schema extraction failed: {e}")

    # 4. embeddings (optional until Phase 3)
    embed_info = None
    if s.embed_model:
        try:
            t0 = time.perf_counter()
            vecs = llm.embed(["AMZN MKTP IN*2K4", "Amazon shopping"])
            embed_info = {"dim": len(vecs[0]), "s": round(time.perf_counter() - t0, 3)}
            ok(f"embeddings dim={embed_info['dim']}")
        except Exception as e:  # noqa: BLE001
            fail(f"embeddings failed: {e}")
    else:
        print("  - embeddings skipped (EMBED_MODEL not set; needed from Phase 3)")

    if args.no_baseline:
        return 0

    # 5. latency baseline
    prompt = [{"role": "user", "content": "In about 150 words, explain what a bank statement "
                                          "reconciliation is and why it matters."}]
    runs = []
    for i in range(args.runs):
        st = llm.chat_stream_stats(prompt, max_tokens=512)
        runs.append(st)
        print(f"  run {i + 1}: ttft {st.ttft_s:.2f}s  decode {st.decode_tok_per_s:.1f} tok/s  "
              f"({st.completion_tokens} tok, {st.total_s:.1f}s)")
    med = lambda xs: round(statistics.median(xs), 3)
    result = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "model": s.chat_model,
        "provider": s.llm_provider,
        "extra_body": s.llm_extra_body,
        "machine": {"chip": _chip(), "ram_gb": _ram_gb(), "macos": platform.mac_ver()[0]},
        "first_call_s": round(cold, 2),
        "ttft_s_median": med([r.ttft_s for r in runs]),
        "decode_tok_per_s_median": med([r.decode_tok_per_s for r in runs]),
        "total_s_median": med([r.total_s for r in runs]),
        "extract_6_rows": {"seconds": extract_s and round(extract_s, 2),
                           "rows": len(rows), "balances_exact": bal_hits},
        "embeddings": embed_info,
    }
    slug = s.chat_model.replace("/", "_")
    out = s.data_dir / "baselines" / f"latency_{s.llm_provider}_{slug}_{datetime.now():%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    ok(f"baseline -> {out.relative_to(s.data_dir.parent)}")
    print(f"    median ttft {result['ttft_s_median']}s, decode "
          f"{result['decode_tok_per_s_median']} tok/s")
    return 0


def _chip() -> str:
    try:
        return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    except Exception:  # noqa: BLE001
        return platform.machine()


def _ram_gb() -> int:
    try:
        return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) // 2**30
    except Exception:  # noqa: BLE001
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
