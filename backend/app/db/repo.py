"""Repository functions. Plain SQL via psycopg; all money in paise."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
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


SAME_STATEMENT_SHARE = 0.9  # ≥90% of rows match both ways → the same statement again


def txn_key(date, debit: int, credit: int, balance: int | None, description: str) -> tuple:
    """Identity of a transaction within an account. The running balance makes it practically
    unique (two equal payments on one day still differ in balance). Without a balance, the
    narration (normalised) is used instead."""
    if balance is not None:
        return (str(date), debit, credit, balance)
    return (str(date), debit, credit, None, re.sub(r"[^A-Z0-9]", "", (description or "").upper()))


def _match_existing(conn: psycopg.Connection, rows: list[Txn], account: str | None,
                    exclude: str, only: list[str] | None = None,
                    ) -> tuple[list[int | None], dict[str, int], dict[str, int]]:
    """For each new row, the id of the same transaction already stored for this account (or
    None). Also: matches per existing statement, and each statement's original-row count."""
    dates = [t.date for t in rows if t.date]
    if not dates:
        return [None] * len(rows), {}, {}
    acct_sql, p = "", {"lo": min(dates), "hi": max(dates), "ex": exclude}
    if account:
        acct_sql, p["acct"] = "AND s.account_masked = %(acct)s", account
    if only is not None:
        acct_sql += " AND s.id = ANY(%(only)s)"
        p["only"] = only
    pool: dict[tuple, list[tuple[int, str]]] = defaultdict(list)
    for r in conn.execute(
            f"""SELECT t.id, t.statement_id, t.date, t.debit, t.credit, t.balance, t.description
                FROM transactions t JOIN statements s ON s.id = t.statement_id
                WHERE t.duplicate_of IS NULL AND s.id <> %(ex)s {acct_sql}
                  AND t.date BETWEEN %(lo)s AND %(hi)s ORDER BY t.id""", p):
        pool[txn_key(r["date"], r["debit"], r["credit"], r["balance"], r["description"])].append(
            (r["id"], r["statement_id"]))
    matches: list[int | None] = []
    per_stmt: dict[str, int] = defaultdict(int)
    for t in rows:
        hit = pool.get(txn_key(t.date, t.debit, t.credit, t.balance, t.description))
        if hit:
            tid, sid = hit.pop(0)
            matches.append(tid)
            per_stmt[sid] += 1
        else:
            matches.append(None)
    sizes = {r["statement_id"]: r["n"] for r in conn.execute(
        """SELECT statement_id, count(*) AS n FROM transactions
           WHERE duplicate_of IS NULL AND statement_id = ANY(%s) GROUP BY 1""",
        (list(per_stmt),))} if per_stmt else {}
    return matches, dict(per_stmt), sizes


def _delete_statements(conn: psycopg.Connection, ids: list[str]) -> list[str]:
    """Delete statements; return the other statements whose copies become originals (their
    rows pointed at the deleted ones), which then need re-indexing."""
    if not ids:
        return []
    dependents = [r["statement_id"] for r in conn.execute(
        """SELECT DISTINCT d.statement_id FROM transactions d
           JOIN transactions o ON o.id = d.duplicate_of
           WHERE o.statement_id = ANY(%s) AND d.statement_id <> ALL(%s)""", (ids, ids))]
    conn.execute("DELETE FROM statements WHERE id = ANY(%s)", (ids,))
    return dependents


def save_statement(conn: psycopg.Connection, stmt_id: str, pdf: str, meta: StatementMeta,
                   rows: list[Txn], status: str, source_type: str,
                   report: dict) -> tuple[list[int], dict]:
    """Insert or replace a statement and its transactions, without ever double-counting.

    - The same file again (same id / sha256) replaces its earlier copy.
    - The same statement as a different file (≥90% of rows match both ways, same account)
      replaces the earlier copy, except that a digital original is kept over a scanned copy:
      then nothing is inserted and info["duplicate_of"] names the statement kept.
    - Overlapping periods: rows already stored are inserted with duplicate_of → the original;
      every reader skips them.
    Returns (transaction ids in row order, info: replaced, duplicate_rows, duplicate_of,
    reindex = other statements whose rows became originals and need re-indexing)."""
    sha = file_sha256(pdf)
    own = [r["id"] for r in conn.execute(
        "SELECT id FROM statements WHERE id = %s OR file_sha256 = %s", (stmt_id, sha))]
    reindex = _delete_statements(conn, own)
    matches, per_stmt, sizes = _match_existing(conn, rows, meta.account_masked, stmt_id)
    n = len(rows)
    same = [s for s, m in per_stmt.items()
            if n and m >= SAME_STATEMENT_SHARE * n and m >= SAME_STATEMENT_SHARE * sizes.get(s, 0)]
    info: dict = {"replaced": [], "duplicate_rows": 0, "duplicate_of": None, "reindex": []}
    if same:
        old = conn.execute("SELECT id, source_type FROM statements WHERE id = ANY(%s) "
                           "ORDER BY created_at LIMIT 1", (same,)).fetchone()
        if old["source_type"] == "digital" and source_type != "digital":
            conn.commit()
            return [], info | {"duplicate_of": old["id"], "reindex": reindex}
        reindex += _delete_statements(conn, same)
        info["replaced"] = same
        matches, _, _ = _match_existing(conn, rows, meta.account_masked, stmt_id)
    info["duplicate_rows"] = sum(m is not None for m in matches)
    info["reindex"] = sorted(set(reindex) - set(same) - {stmt_id})
    conn.execute(
        """INSERT INTO statements (id, file_name, file_sha256, bank, account_holder,
               account_masked, period_start, period_end, opening_balance, closing_balance,
               currency, source_type, status, reconciliation, dedupe)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (stmt_id, Path(pdf).name, sha, meta.bank_name, meta.account_holder, meta.account_masked,
         meta.period_start, meta.period_end, meta.opening_balance, meta.closing_balance,
         meta.currency, source_type, status, Jsonb(_compact_report(report)), Jsonb(info)))
    ids = []
    with conn.cursor() as cur:
        for i, (t, dup) in enumerate(zip(rows, matches, strict=True)):
            cur.execute(
                """INSERT INTO transactions (statement_id, idx, page, bbox, date, value_date,
                       description, ref, debit, credit, balance, source, notes, user_corrected,
                       duplicate_of)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (stmt_id, i, t.page, list(t.bbox) if t.bbox else None, t.date, t.value_date,
                 t.description, t.ref, t.debit, t.credit, t.balance, t.source, t.notes,
                 "user_corrected" in t.notes, dup))
            ids.append(cur.fetchone()["id"])
    conn.commit()
    return ids, info


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
