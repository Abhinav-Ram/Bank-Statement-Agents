"""Phase 2 end-to-end on a local test database (no Groq: LLM disabled). Skips if Postgres isn't up."""

import psycopg
import pytest

from app.config import get_settings
from app.datasets.truth import iter_dataset

TEST_DB = "bank_agents_test"


def _test_url() -> str | None:
    base = get_settings().database_url
    admin = base.rsplit("/", 1)[0] + "/postgres"
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=2) as c:
            if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
                c.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.Error:
        return None
    return base.rsplit("/", 1)[0] + "/" + TEST_DB


URL = _test_url()
SYN = iter_dataset("synthetic", get_settings().data_dir)
pytestmark = pytest.mark.skipif(not URL or not SYN, reason="needs local Postgres + synthetic data")


@pytest.fixture
def env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "database_url", URL)
    monkeypatch.setattr(s, "llm_disabled", True)
    from app.db import connect, migrate
    with connect() as conn:
        migrate(conn)
        conn.execute("TRUNCATE statements, merchant_cache CASCADE")
        conn.commit()
    return s


def truth_meta(tr):
    return tr.meta.model_dump()


def test_migrate_is_idempotent_and_seeds_taxonomy(env):
    from app.db import category_ids, connect, migrate
    from app.schemas.taxonomy import ALL_CATEGORIES
    with connect() as conn:
        migrate(conn)
        migrate(conn)
        assert set(category_ids(conn)) == set(ALL_CATEGORIES)


def test_full_pipeline_persists_categorises_flags_and_summarises(env):
    from app.db import connect
    from app.graphs.ingestion import build_graph, start_ingestion

    tr = next(t for t in SYN if t.id.startswith("coral_03"))  # 3 months, all anomaly types
    out = start_ingestion(build_graph(), str(tr.pdf), review_mode="skip",
                          meta_override=truth_meta(tr))
    st = out["state"]
    assert out["interrupt"] is None and st["status"] == "ok"
    assert [e["node"] for e in st["events"]][-4:] == ["persist", "categorize", "anomaly",
                                                      "summarize"]
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n, count(category_id) AS c FROM transactions "
                         "WHERE statement_id = %s", (out["stmt_id"],)).fetchone()
        assert n["n"] == len(tr.rows) and n["c"] == n["n"]
        types = {r["type"] for r in conn.execute(
            "SELECT type FROM anomalies WHERE statement_id = %s", (out["stmt_id"],))}
        assert {"duplicate_charge", "large_debit", "fee_spike", "new_recurring"} <= types
        summ = conn.execute("SELECT narrative_source, fact_sheet FROM summaries "
                            "WHERE statement_id = %s", (out["stmt_id"],)).fetchone()
        assert summ["narrative_source"] == "template"
        assert summ["fact_sheet"]["totals"]["transactions"] > 0
    # category accuracy vs the generator's labels (rules only, no LLM)
    labels = [r for r in __import__("json").loads(tr.pdf.with_suffix(".json").read_text())[
        "transactions"]]
    got = [a["category"] for a in st["categories"]]
    acc = sum(g == lab["category"] for g, lab in zip(got, labels, strict=True)) / len(labels)
    assert acc >= 0.85, acc


def test_reingesting_same_file_replaces_rows(env):
    from app.db import connect
    from app.graphs.ingestion import build_graph, start_ingestion

    tr = SYN[0]
    g = build_graph()
    a = start_ingestion(g, str(tr.pdf), review_mode="skip", meta_override=truth_meta(tr))
    start_ingestion(g, str(tr.pdf), review_mode="skip", meta_override=truth_meta(tr))
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM statements").fetchone()["n"]
        m = conn.execute("SELECT count(*) AS n FROM transactions WHERE statement_id = %s",
                         (a["stmt_id"],)).fetchone()["n"]
    assert n == 1 and m == len(tr.rows)


def test_user_category_correction_wins_over_automatic_sources(env):
    from app.db import connect
    from app.db.repo import cache_lookup, cache_put, correct_category
    from app.graphs.ingestion import build_graph, start_ingestion

    tr = SYN[0]
    out = start_ingestion(build_graph(), str(tr.pdf), review_mode="skip",
                          meta_override=truth_meta(tr))
    with connect() as conn:
        row = conn.execute("SELECT id, merchant FROM transactions WHERE statement_id = %s "
                           "AND debit > 0 ORDER BY idx LIMIT 1", (out["stmt_id"],)).fetchone()
        correct_category(conn, row["id"], "Health")
        cache_put(conn, [(row["merchant"], "Shopping", 0.9, "llm")])  # must not overwrite
        assert cache_lookup(conn, [row["merchant"]])[row["merchant"]][0] == "Health"


def test_review_gate_pauses_and_resume_continues(env):
    """Code path only (automated reviewer): a wrong closing balance pauses the graph; resuming
    with accept=True finishes it and persists the statement."""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.db import connect
    from app.graphs.ingestion import build_graph, resume_ingestion, start_ingestion

    tr = SYN[0]
    meta = truth_meta(tr)
    meta["closing_balance"] += 100  # totals won't reconcile → review
    g = build_graph(checkpointer=InMemorySaver())
    out = start_ingestion(g, str(tr.pdf), review_mode="interrupt", meta_override=meta)
    assert out["interrupt"] is not None and out["interrupt"]["errors"]
    with connect() as conn:  # nothing persisted while paused
        assert conn.execute("SELECT count(*) AS n FROM statements").fetchone()["n"] == 0
    done = resume_ingestion(g, out["stmt_id"], {"accept": True})
    assert done["interrupt"] is None and done["state"]["status"] == "ok"
    with connect() as conn:
        assert conn.execute("SELECT status FROM statements WHERE id = %s",
                            (out["stmt_id"],)).fetchone()["status"] == "ok"


def test_paused_review_shows_readable_issues_and_a_suggestion_can_be_sent_back(env):
    """The interrupt payload is a ReviewRequest; sending its suggested fix back finishes it."""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.graphs.ingestion import build_graph, resume_ingestion, start_ingestion
    from app.services.review import ReviewRequest

    tr = SYN[0]
    meta = truth_meta(tr)
    meta["closing_balance"] += 123_45
    g = build_graph(checkpointer=InMemorySaver())
    out = start_ingestion(g, str(tr.pdf), review_mode="interrupt", meta_override=meta)
    req = ReviewRequest.model_validate(out["interrupt"])
    (iss,) = req.issues
    assert iss.kind == "closing_balance" and iss.suggestions[0].likely
    assert req.statement.file_name == tr.pdf.name and "₹" in iss.explanation
    sugg = iss.suggestions[0]
    done = resume_ingestion(g, out["stmt_id"], {"action": "correct",
                                                "meta": sugg.meta.model_dump(),
                                                "note": "closing misread"})
    assert done["interrupt"] is None and done["state"]["status"] == "ok"
    assert done["state"]["report"]["ok"]  # re-checked, not just waved through
    assert done["state"]["review_log"][0]["action"] == "correct"


def _amount_typo_case(tmp_path):
    """Synthetic statement with one debit printed with two digits swapped (a printed typo)."""
    import sys
    sys.path.insert(0, "scripts")
    from make_review_cases import replace_cell, swap_digits

    from app.parsing.digital import parse_pdf
    tr = next(t for t in SYN if t.id.startswith("summit_01"))
    rows = parse_pdf(tr.pdf).rows
    i = next(k for k in range(8, len(rows)) if rows[k].debit and rows[k].page == 1)
    old = rows[i].raw["debit"]
    pdf = tmp_path / "typo.pdf"
    replace_cell(tr.pdf, pdf, rows[i].page, rows[i].bbox, old, swap_digits(old))
    return pdf, truth_meta(tr), i, old


def test_a_wrong_correction_is_asked_again_even_if_other_cards_were_kept(env, tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver

    from app.graphs.ingestion import build_graph, resume_ingestion, start_ingestion

    pdf, meta, i, true_amount = _amount_typo_case(tmp_path)
    g = build_graph(checkpointer=InMemorySaver())
    out = start_ingestion(g, str(pdf), review_mode="interrupt", meta_override=meta)
    ids = {x["id"] for x in out["interrupt"]["issues"]}
    assert ids == {f"row-{i}", "closing"}
    # reviewer types a wrong value for the row and keeps the follow-on totals card
    again = resume_ingestion(g, out["stmt_id"], {"action": "correct", "keep": ["closing"],
                                                 "rows": [{"idx": i, "debit": "1.00"}]})
    assert again["interrupt"] is not None  # NOT saved: the edited row still doesn't add up
    assert f"row-{i}" in {x["id"] for x in again["interrupt"]["issues"]}
    # a value that isn't an amount is reported back, nothing is applied
    bad = resume_ingestion(g, out["stmt_id"], {"action": "correct",
                                               "rows": [{"idx": i, "debit": "abc"}]})
    assert "couldn't be used" in bad["interrupt"]["previous_answer_error"]
    done = resume_ingestion(g, out["stmt_id"], {"action": "correct",
                                                "rows": [{"idx": i, "debit": true_amount}]})
    assert done["interrupt"] is None and done["state"]["status"] == "ok"
    assert done["state"]["report"]["ok"]


def test_keeping_cards_as_printed_saves_without_asking_again(env, tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver

    from app.graphs.ingestion import build_graph, resume_ingestion, start_ingestion

    pdf, meta, i, _ = _amount_typo_case(tmp_path)
    g = build_graph(checkpointer=InMemorySaver())
    out = start_ingestion(g, str(pdf), review_mode="interrupt", meta_override=meta)
    done = resume_ingestion(g, out["stmt_id"], {"action": "correct",
                                                "keep": [f"row-{i}", "closing"]})
    assert done["interrupt"] is None and done["state"]["status"] == "ok"
    assert not done["state"]["report"]["ok"]  # kept as printed, honestly not reconciled


def test_batch_mode_stores_the_review_for_later(env):
    from app.db import connect
    from app.graphs.ingestion import build_graph, start_ingestion

    tr = SYN[0]
    meta = truth_meta(tr)
    meta["closing_balance"] += 100
    out = start_ingestion(build_graph(), str(tr.pdf), review_mode="skip", meta_override=meta)
    with connect() as conn:
        r = conn.execute("SELECT status, reconciliation FROM statements WHERE id = %s",
                         (out["stmt_id"],)).fetchone()
    assert r["status"] == "awaiting_review"
    assert r["reconciliation"]["review"]["issues"][0]["kind"] == "closing_balance"


def test_corrections_are_applied_and_revalidated():
    from app.graphs.ingestion import apply_corrections
    from app.schemas.statement import Txn

    rows = [Txn(date="2025-01-01", debit=100, balance=900, ambiguous=["no_amount"])]
    out = apply_corrections(rows, [{"idx": 0, "debit": 200, "balance": 800, "bogus": 1},
                                   {"idx": 9, "debit": 1}])
    assert out[0].debit == 200 and out[0].balance == 800 and out[0].source == "user"
    assert "user_corrected" in out[0].notes and out[0].ambiguous == []
