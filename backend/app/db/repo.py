"""Repository functions. Plain SQL via psycopg; all money in paise."""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.db import category_ids
from app.schemas.statement import StatementMeta, Txn


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def statement_id_for(path: str | Path) -> str:
    return "st_" + file_sha256(path)[:16]


def save_statement(conn: psycopg.Connection, stmt_id: str, pdf: str, meta: StatementMeta,
                   rows: list[Txn], status: str, source_type: str, report: dict) -> list[int]:
    """Insert or replace a statement and its transactions. Returns transaction ids in row order."""
    sha = file_sha256(pdf)
    conn.execute("DELETE FROM statements WHERE id = %s OR file_sha256 = %s", (stmt_id, sha))
    conn.execute(
        """INSERT INTO statements (id, file_name, file_sha256, bank, account_holder,
               account_masked, period_start, period_end, opening_balance, closing_balance,
               currency, source_type, status, reconciliation)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (stmt_id, Path(pdf).name, sha, meta.bank_name, meta.account_holder, meta.account_masked,
         meta.period_start, meta.period_end, meta.opening_balance, meta.closing_balance,
         meta.currency, source_type, status, Jsonb(_compact_report(report))))
    ids = []
    with conn.cursor() as cur:
        for i, t in enumerate(rows):
            cur.execute(
                """INSERT INTO transactions (statement_id, idx, page, bbox, date, value_date,
                       description, ref, debit, credit, balance, source, notes, user_corrected)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (stmt_id, i, t.page, list(t.bbox) if t.bbox else None, t.date, t.value_date,
                 t.description, t.ref, t.debit, t.credit, t.balance, t.source, t.notes,
                 "user_corrected" in t.notes))
            ids.append(cur.fetchone()["id"])
    conn.commit()
    return ids


def _compact_report(report: dict) -> dict:
    keys = ("ok", "opening_ok", "totals_ok", "closing_matches_last", "errors")
    out = {k: report.get(k) for k in keys}
    out["n_breaks"] = len(report.get("breaks", []))
    out["break_kinds"] = sorted({b["kind"] for b in report.get("breaks", [])})
    out["review_log"] = report.get("review_log", [])
    if report.get("review"):  # awaiting review: what the review screen shows
        out["review"] = report["review"]
    return out


def set_status(conn: psycopg.Connection, stmt_id: str, status: str) -> None:
    conn.execute("UPDATE statements SET status = %s, updated_at = now() WHERE id = %s",
                 (status, stmt_id))
    conn.commit()


# ----------------------------------------------------------------- categories / merchant cache
def set_categories(conn: psycopg.Connection, assignments: list[dict]) -> None:
    """assignments: {id, merchant, category, confidence, source}"""
    cid = category_ids(conn)
    with conn.cursor() as cur:
        cur.executemany(
            """UPDATE transactions SET merchant = %s, category_id = %s, category_confidence = %s,
                   category_source = %s WHERE id = %s""",
            [(a["merchant"], cid[a["category"]] if a.get("category") else None,
              a.get("confidence"), a.get("source"), a["id"]) for a in assignments])
    conn.commit()


def cache_lookup(conn: psycopg.Connection, merchants: list[str]) -> dict[str, tuple[str, float]]:
    if not merchants:
        return {}
    rows = conn.execute(
        """SELECT m.merchant, c.name, m.confidence FROM merchant_cache m
           JOIN categories c ON c.id = m.category_id WHERE m.merchant = ANY(%s)""",
        (merchants,)).fetchall()
    return {r["merchant"]: (r["name"], r["confidence"]) for r in rows}


def cache_put(conn: psycopg.Connection, entries: list[tuple[str, str, float, str]]) -> None:
    """entries: (merchant, category, confidence, source). A user correction always wins;
    automatic sources never overwrite a user entry."""
    cid = category_ids(conn)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO merchant_cache (merchant, category_id, confidence, source)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (merchant) DO UPDATE SET category_id = EXCLUDED.category_id,
                   confidence = EXCLUDED.confidence, source = EXCLUDED.source, updated_at = now()
               WHERE merchant_cache.source <> 'user' OR EXCLUDED.source = 'user'""",
            [(m, cid[c], conf, src) for m, c, conf, src in entries if c in cid])
    conn.commit()


def correct_category(conn: psycopg.Connection, txn_id: int, category: str) -> None:
    """User correction (PATCH /transactions/{id} in Phase 4): updates the row and the cache."""
    cid = category_ids(conn)[category]
    row = conn.execute("UPDATE transactions SET category_id = %s, category_confidence = 1.0, "
                       "category_source = 'user', user_corrected = true WHERE id = %s "
                       "RETURNING merchant", (cid, txn_id)).fetchone()
    conn.commit()
    if row and row["merchant"]:
        cache_put(conn, [(row["merchant"], category, 1.0, "user")])


# ----------------------------------------------------------------- history / anomalies / summary
def earlier_merchants(conn: psycopg.Connection, account_masked: str | None,
                      before: str | None, exclude_stmt: str) -> set[str]:
    """Merchants seen on the same account in statements that ended before `before`."""
    if not account_masked or not before:
        return set()
    rows = conn.execute(
        """SELECT DISTINCT t.merchant FROM transactions t JOIN statements s ON s.id = t.statement_id
           WHERE s.account_masked = %s AND s.id <> %s AND t.date < %s AND t.merchant IS NOT NULL""",
        (account_masked, exclude_stmt, before)).fetchall()
    return {r["merchant"] for r in rows}


def save_anomalies(conn: psycopg.Connection, stmt_id: str, items: list[dict]) -> None:
    conn.execute("DELETE FROM anomalies WHERE statement_id = %s", (stmt_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO anomalies (statement_id, transaction_id, type, score, details, explanation)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            [(stmt_id, a.get("transaction_id"), a["type"], a["score"], Jsonb(a.get("details", {})),
              a.get("explanation")) for a in items])
    conn.commit()


def save_summary(conn: psycopg.Connection, stmt_id: str, period: str, fact_sheet: dict,
                 narrative: str, source: str) -> None:
    conn.execute(
        """INSERT INTO summaries (statement_id, period, fact_sheet, narrative, narrative_source)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (statement_id) DO UPDATE SET period = EXCLUDED.period,
               fact_sheet = EXCLUDED.fact_sheet, narrative = EXCLUDED.narrative,
               narrative_source = EXCLUDED.narrative_source""",
        (stmt_id, period, Jsonb(fact_sheet), narrative, source))
    conn.commit()
