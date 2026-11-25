# Phase 2: user-led review testing

The terminal review tool stands in for the Phase 5 review screen. It uses the same
ReviewRequest/ReviewAnswer contract. No Groq calls are made: metadata comes from a sidecar file
or the dataset's ground truth, and later stages use rules and templates.

## Setup (once)
```bash
docker compose up -d db
cd backend
uv run python scripts/make_review_cases.py    # → data/review_cases/ (+ ANSWERS.md: peek after)
```

## How a review works
- Each flagged item is a card. A card shows:
  - what doesn't add up, in ₹;
  - the rows before and after, so you can follow the running balance;
  - how the row was read;
  - suggested fixes, with the likely one marked.
- A page image is saved with the row boxed in red. The path is printed on the card; open it, or
  open the PDF at that page.
- Keys: `1`/`2` use a suggestion · `k` keep as printed · `e` type values yourself (rupees, e.g.
  `1,234.50`, `500 Dr`) · `s` decide later · `K` keep all remaining as printed · `q` quit.
- After you answer, it re-checks everything. If something still doesn't add up, it asks again.
  Otherwise it saves, categorizes, flags anomalies and prints the summary.
- The pause is saved in Postgres. After `q`, run the same command to continue, or add `--fresh`
  to start over.

## Cases to try
| # | Command (from `backend/`) | What to judge |
|---|---|---|
| 1 | `uv run python scripts/review.py ../data/review_cases/amount_typo.pdf` | Does the card take you to the right row? Is the first suggestion the right fix? |
| 2 | `uv run python scripts/review.py ../data/review_cases/balance_typo.pdf` | Two cards: the second should say it follows from the first. |
| 3 | `uv run python scripts/review.py ../data/review_cases/closing_misread.pdf` | No row cards; one closing-balance card. Try `q` midway, then continue. |
| 4 | `uv run python scripts/review.py --dataset agami_digital_t1 --index 0` | Real-style statement that prints inconsistent balances. Spot-check a few cards against the PDF, then `K`. |
| 5 | `uv run python scripts/review.py --dataset agami_scanned_t1 --index 0` | The same statement scanned (OCR takes about 1 min). Does the red box sit on the right row of the scan? |
| 6 | Case 1 again with `--fresh`, using `e` to type the amount yourself | Is typing values natural? Is a wrong value caught and asked again? |

What to note for each case:
- Was the card understandable without explanation?
- Did the picture plus the text get you to the right place quickly?
- Was the suggested fix right?
- Anything confusing in the wording?

After a case, `data/review_cases/ANSWERS.md` has the truth.
