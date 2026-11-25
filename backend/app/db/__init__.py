"""Postgres access: connection, idempotent schema migration, category seeding."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.schemas.taxonomy import EXPENSE, INCOME

SCHEMA = Path(__file__).with_name("schema.sql")


@contextmanager
def connect(url: str | None = None):
    with psycopg.connect(url or get_settings().database_url, row_factory=dict_row) as conn:
        yield conn


def migrate(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO categories (head, name) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            [("Income", n) for n in INCOME] + [("Expense", n) for n in EXPENSE])
    conn.commit()


def category_ids(conn: psycopg.Connection) -> dict[str, int]:
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
