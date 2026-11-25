"""Terminal review screen: run a statement through the full pipeline and review it by hand.

    uv run python scripts/review.py ../data/review_cases/amount_typo.pdf
    uv run python scripts/review.py --dataset agami_digital_t1 --index 0
    uv run python scripts/review.py <pdf> --fresh        # discard a saved pause, start over

Stand-in for the Phase 5 review screen, using the same ReviewRequest / ReviewAnswer contract.
The pause is saved in Postgres: quit with q and run the same command again to continue.
For each flagged row a page image with the row boxed in red is written to data/review/<id>/.

No Groq calls by default: metadata comes from <pdf>.meta.json or the dataset's ground truth,
and the later stages use rules + templates. --allow-groq turns the LLM on (uses quota).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.services.review import Issue, MetaEdit, ReviewAnswer, ReviewRequest, RowEdit

TTY = sys.stdout.isatty()


def b(s: str) -> str:
    return f"\033[1m{s}\033[0m" if TTY else s


def dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if TTY else s


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


# ------------------------------------------------------------------ page image
def row_image(pdf: str, issue: Issue, out_dir: Path) -> Path | None:
    loc = issue.location
    if not loc or not loc.bbox or not loc.page_width:
        return None
    doc = pymupdf.open(pdf)
    page = doc[loc.page - 1]
    k = page.rect.width / loc.page_width  # bbox units → this page's PDF points
    x0, y0, x1, y1 = (v * k for v in loc.bbox)
    rect = pymupdf.Rect(x0 - 3, y0 - 2, x1 + 3, y1 + 2)
    page.draw_rect(rect, color=(0.85, 0.1, 0.1), width=1.6)
    clip = pymupdf.Rect(0, max(0, y0 - 110), page.rect.width, min(page.rect.height, y1 + 110))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{issue.id}.png"
    page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip).save(path)
    return path


# ------------------------------------------------------------------ printing
def show_request(req: ReviewRequest) -> None:
    st = req.statement
    print("\n" + "=" * 78)
    print(b(f"REVIEW · {st.file_name}") + dim(f"  ({st.source_type}, {st.rows} rows)"))
    print(f"{st.bank or ''}  {('a/c ' + st.account) if st.account else ''}  {st.period_text or ''}")
    print(f"Opening {st.opening_balance.text if st.opening_balance else '?'}   "
          f"Closing {st.closing_balance.text if st.closing_balance else '?'}")
    print()
    mark = {"pass": "✓", "fail": "✗", "unknown": "?"}
    for c in req.checks:
        print(f"  {mark[c.status]} {c.label}  {dim(c.detail)}")
    print("\n" + b(req.headline))
    print(dim(req.instructions))


def show_issue(n: int, total: int, iss: Issue, image: Path | None) -> None:
    tag = "LIKELY WRONG" if iss.severity == "fix" else "CONFIRM"
    print("\n" + "-" * 78)
    print(b(f"[{n}/{total}] {iss.title}") + f"   ({tag})")
    if iss.location:
        print(dim(f"Page {iss.location.page}") + (f"  · image: {image}" if image else ""))
    print(iss.explanation)
    if iss.context:
        print()
        print(dim(f"  {'':3}{'row':>4}  {'date':<12}{'description':<34}{'debit':>14}"
                  f"{'credit':>14}{'balance':>16}"))
        for r in iss.context:
            arrow = "→" if r.role == "flagged" else " "
            line = (f"  {arrow:3}{r.row_number:>4}  {r.date_text or '?':<12}"
                    f"{r.description[:33]:<34}{r.debit.text if r.debit else '':>14}"
                    f"{r.credit.text if r.credit else '':>14}"
                    f"{r.balance.text if r.balance else '?':>16}")
            print(b(line) if r.role == "flagged" else line)
        flagged = next(r for r in iss.context if r.role == "flagged")
        print(dim(f"       {flagged.read_by}; printed: "
                  + ", ".join(f"{k}={v!r}" for k, v in flagged.printed.items()
                              if k in ("debit", "credit", "amount", "balance", "drcr"))))
    if iss.suggestions:
        print()
        for k, s in enumerate(iss.suggestions, 1):
            star = " (likely)" if s.likely else ""
            print(f"  {b(str(k))}) {s.label}{star}  {dim(s.why)}")


# ------------------------------------------------------------------ collecting decisions
MENU = ("  {nums}k keep as printed · e edit · s skip (decide later) · "
        "K keep ALL remaining · q quit (resume later)")


def edit_issue(iss: Issue, row_view) -> tuple[RowEdit | None, MetaEdit | None]:
    print(dim("  Type new values (rupees, e.g. 1,234.50 or '500 Dr'); Enter keeps the current."))
    if iss.row_idx is None:
        vals = {}
        for f in iss.editable:
            v = ask(f"    {f}: ")
            if v:
                vals[f] = v
        return None, MetaEdit(**vals) if vals else None
    vals = {}
    for f in iss.editable:
        cur = getattr(row_view, f)
        cur_txt = cur.text if hasattr(cur, "text") else (cur or "")
        v = ask(f"    {f} [{cur_txt}]: ")
        if v:
            vals[f] = v
    return (RowEdit(idx=iss.row_idx, **vals) if vals else None), None


def decide(req: ReviewRequest, pdf: str, img_dir: Path) -> ReviewAnswer | None:
    rows: list[RowEdit] = []
    meta: dict = {}
    kept = skipped = 0
    issues = req.issues
    for n, iss in enumerate(issues, 1):
        show_issue(n, len(issues), iss, row_image(pdf, iss, img_dir))
        nums = f"1-{len(iss.suggestions)} use suggestion · " if iss.suggestions else ""
        while True:
            c = ask(MENU.format(nums=nums) + "\n  > ")
            if c.isdigit() and 1 <= int(c) <= len(iss.suggestions):
                s = iss.suggestions[int(c) - 1]
                rows += s.rows
                if s.meta:
                    meta |= s.meta.model_dump(exclude_none=True)
                print(dim(f"  ✓ will apply: {s.label}"))
                break
            if c == "k":
                kept += 1
                break
            if c == "s":
                skipped += 1
                break
            if c == "e":
                view = next((r for r in iss.context if r.role == "flagged"), None)
                r, m = edit_issue(iss, view)
                if r:
                    rows.append(r)
                if m:
                    meta |= m.model_dump(exclude_none=True)
                if r or m:
                    print(dim("  ✓ edit recorded"))
                    break
                continue
            if c == "K":
                kept += len(issues) - n + 1
                break
            if c == "q":
                return None
            print("  ?")
        if c == "K":
            break
    if not rows and not meta:
        if skipped:
            print("\nNothing decided yet — quitting; run the same command to continue.")
            return None
        answer = ReviewAnswer(action="accept")
    else:
        answer = ReviewAnswer(action="correct", rows=rows, meta=MetaEdit(**meta) if meta else None,
                              keep_remaining=skipped == 0)
    note = ask("\nOptional note for the record (Enter to skip): ")
    answer.note = note or None
    print("\n" + b("Sending: ") + json.dumps(answer.model_dump(exclude_none=True), ensure_ascii=False))
    if ask("Send? [Y/n] ").lower() in ("n", "no", "q"):
        return None
    return answer


# ------------------------------------------------------------------ result
def show_result(stmt_id: str) -> None:
    from app.db import connect

    with connect() as conn:
        st = conn.execute("SELECT status, reconciliation FROM statements WHERE id = %s",
                          (stmt_id,)).fetchone()
        if not st:
            print("Nothing saved.")
            return
        n = conn.execute("SELECT count(*) AS n, count(*) FILTER (WHERE user_corrected) AS u "
                         "FROM transactions WHERE statement_id = %s", (stmt_id,)).fetchone()
        cats = conn.execute(
            "SELECT c.name, count(*) AS n FROM transactions t JOIN categories c "
            "ON c.id = t.category_id WHERE statement_id = %s GROUP BY 1 ORDER BY 2 DESC LIMIT 6",
            (stmt_id,)).fetchall()
        anoms = conn.execute("SELECT type, explanation FROM anomalies WHERE statement_id = %s "
                             "ORDER BY id", (stmt_id,)).fetchall()
        summ = conn.execute("SELECT narrative FROM summaries WHERE statement_id = %s",
                            (stmt_id,)).fetchone()
    rec = st["reconciliation"] or {}
    print("\n" + "=" * 78)
    print(b(f"SAVED · status {st['status']}") + f" · reconciles: {rec.get('ok')} · "
          f"{n['n']} transactions ({n['u']} corrected by you)")
    for r in rec.get("review_log", []):
        print(dim(f"  review round {r['round']}: {r.get('action')} "
                  f"{r.get('rows', '')} {r.get('note') or ''}"))
    print("Top categories: " + ", ".join(f"{c['name']} {c['n']}" for c in cats))
    for a in anoms:
        print(f"  ⚑ {a['type']}: {a['explanation']}")
    if summ:
        print("\n" + summ["narrative"])


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("--dataset")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--fresh", action="store_true", help="discard a saved pause and start over")
    ap.add_argument("--allow-groq", action="store_true", help="let the LLM run (uses quota)")
    args = ap.parse_args()
    s = get_settings()
    s.llm_disabled = not args.allow_groq

    meta = None
    if args.dataset:
        from app.datasets.truth import iter_dataset
        tr = iter_dataset(args.dataset, s.data_dir)[args.index]
        pdf, meta = str(tr.pdf), tr.meta.model_dump()
    elif args.pdf:
        pdf = str(Path(args.pdf).resolve())
        side = Path(pdf).with_suffix(".meta.json")
        meta = json.loads(side.read_text()) if side.exists() else None
    else:
        ap.error("give a PDF or --dataset")
    if meta is None and not args.allow_groq:
        sys.exit("No <pdf>.meta.json next to the file: pass --allow-groq to read the metadata "
                 "with the LLM (do not use Groq for real statements unmasked).")

    from app.db.repo import statement_id_for
    from app.graphs.ingestion import (
        _snapshot,
        build_graph,
        postgres_checkpointer,
        resume_ingestion,
        start_ingestion,
    )

    stmt_id = statement_id_for(pdf)
    img_dir = s.data_dir / "review" / stmt_id
    with postgres_checkpointer() as saver:
        g = build_graph(checkpointer=saver)
        out = _snapshot(g, stmt_id)
        if out["interrupt"] and not args.fresh:
            print(dim(f"Continuing the saved review of {stmt_id}."))
        else:
            saver.delete_thread(stmt_id)
            print(dim(f"Processing {Path(pdf).name} … (statement id {stmt_id})"))
            out = start_ingestion(g, pdf, review_mode="interrupt", meta_override=meta)
        rounds = 0
        while out["interrupt"]:
            rounds += 1
            req = ReviewRequest.model_validate(out["interrupt"])
            if rounds > 1:
                print("\n" + b("Re-checked after your changes — something still doesn't add up."))
            show_request(req)
            answer = decide(req, pdf, img_dir)
            if answer is None:
                print(dim("Paused. Run the same command to continue (or --fresh to restart)."))
                return
            out = resume_ingestion(g, stmt_id, answer.model_dump(mode="json"))
    if rounds == 0:
        print(b("No review needed: the statement adds up."))
    show_result(stmt_id)


if __name__ == "__main__":
    main()
