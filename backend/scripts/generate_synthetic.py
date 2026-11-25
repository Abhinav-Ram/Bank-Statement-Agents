"""Generate synthetic statements + ground truth.

    uv run --group synth python scripts/generate_synthetic.py            # defaults
    uv run --group synth python scripts/generate_synthetic.py --per-layout 4 --scans

Output (data/synthetic/):
    <id>.pdf          digital statement (text layer)
    <id>.json         ground truth (metadata, transactions w/ page, categories, anomalies)
    <id>_scan.pdf     image-only degraded copy (with --scans); same ground truth
    manifest.json     index of everything generated
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.synthetic.generator import generate_statement
from app.synthetic.render import BANKS, render, simulate_scan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-layout", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scans", action="store_true", help="also write simulated scanned copies")
    ap.add_argument("--out", type=Path, default=get_settings().data_dir / "synthetic")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    qualities = ["good", "medium", "poor"]
    manifest = []
    for li, (layout, bank) in enumerate(BANKS.items()):
        for k in range(args.per_layout):
            seed = args.seed * 1000 + li * 100 + k
            sid = f"{layout}_{k + 1:02d}"
            months = 1 + (k % 3)  # 1-3 month statements -> 1 to several pages
            stmt = generate_statement(sid, layout, bank, seed, months=months)
            pdf = render(stmt)  # sets txn.page
            (args.out / f"{sid}.pdf").write_bytes(pdf)
            gt = stmt.to_ground_truth()
            (args.out / f"{sid}.json").write_text(json.dumps(gt, indent=2))
            entry = {"id": sid, "layout": layout, "pdf": f"{sid}.pdf", "truth": f"{sid}.json",
                     "months": months, "n_txns": gt["n_transactions"],
                     "pages": max(t["page"] for t in gt["transactions"])}
            if args.scans:
                q = qualities[k % 3]
                (args.out / f"{sid}_scan.pdf").write_bytes(simulate_scan(pdf, seed, quality=q))
                entry |= {"scan": f"{sid}_scan.pdf", "scan_quality": q}
            manifest.append(entry)
            print(f"{sid:14s} {months}mo {entry['n_txns']:3d} txns {entry['pages']}p"
                  + (f"  scan={entry['scan_quality']}" if args.scans else ""))
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(manifest)} statements to {args.out}")


if __name__ == "__main__":
    main()
