"""One LLM call writes a one-line explanation per flagged anomaly. Any explanation containing a
number that isn't in the flagged item is replaced by the deterministic template."""

from __future__ import annotations

import json

from pydantic import BaseModel

from app.llm.client import LLMClient
from app.services.anomaly import allowed_numbers, numbers_in, template_explanation

SYSTEM = ("Write a one-sentence (max 20 words) plain-English explanation for each flagged bank "
          "transaction, for the account holder. Use only the facts given; amounts in rupees (₹). "
          "Say what the data shows; never guess a cause or reason that isn't in the facts "
          "(no 'due to a bank error', 'for the same order', etc.).")


class _Line(BaseModel):
    id: int
    explanation: str


class _Lines(BaseModel):
    items: list[_Line]


def explain(anomalies: list[dict], rows_by_idx: dict[int, dict], llm: LLMClient | None) -> list[dict]:
    for a in anomalies:
        a["explanation"] = template_explanation(a, rows_by_idx[a["idx"]])
        a["explanation_source"] = "template"
    if not anomalies or llm is None:
        return anomalies
    payload = [{"id": k, "type": a["type"], "date": rows_by_idx[a["idx"]].get("date"),
                "description": rows_by_idx[a["idx"]].get("description", "")[:80],
                **{key: (v / 100 if key in {"amount", "category_median", "month_fees",
                                            "typical_month"} and isinstance(v, int) else v)
                   for key, v in a["details"].items()}}
               for k, a in enumerate(anomalies)]
    try:
        res = llm.chat_json([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                            _Lines, max_tokens=min(900, 120 + 70 * len(payload)))
    except Exception:  # noqa: BLE001 - templates stay
        return anomalies
    for line in res.items:
        if 0 <= line.id < len(anomalies):
            a = anomalies[line.id]
            ok_nums = allowed_numbers(a, rows_by_idx[a["idx"]])
            if line.explanation.strip() and numbers_in(line.explanation) <= ok_nums:
                a["explanation"], a["explanation_source"] = line.explanation.strip(), "llm"
    return anomalies
