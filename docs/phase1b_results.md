# Phase 1b results (2026-11-25)

OCR: **Apple Vision** (default) · re-read second opinion: Tesseract · no Groq calls
(`--no-llm`, ground-truth metadata). Subset: 5 synthetic scans + 5 AgamiAI Scanned Type1 +
5 Type2. The OCR engine bake-off (Vision vs Tesseract vs RapidOCR) is deferred.

## Full scanned pipeline (parse → validate → cascade re-read → review gate) — final
Shear correction + cascade retry (Vision 2× first; Tesseract only if the two Vision readings
disagree; majority vote). No Groq calls.

| Dataset | Stmts | Row recall | Rows exact | Debit/Credit | Balance | Description | Reconciled (ok) | Truth's own | s/stmt |
|---|---|---|---|---|---|---|---|---|---|
| Synthetic scans (good/med/poor) | 5 | 93.2% | 60.4% | 100% | 97.1% | 67.0% | 1/5 | 5/5 | 1.0 |
| AgamiAI Scanned Type1 | 5 | **100%** | 88.0% | **100%** | **100%** | 88.0% | 0/5 | 0/5 | 8.0 |
| AgamiAI Scanned Type2 | 5 | **99.8%** | 97.5% | **100%** | 99.9% | 97.9% | 0/5 | 0/5 | 9.2 |

Before these two fixes: Type1 recall 96.4%, balance 99.5%; Type2 recall 95.4%, rows exact 91.8%.

- **Every statement that doesn't reconcile stopped at the review gate**; nothing unreconciled was
  passed as `ok`. AgamiAI can't reach `ok` because its own ground truth doesn't reconcile.
- Cascade on 188 balance breaks: **181 confirmed as printed** (Vision read the same value twice →
  the statement itself doesn't add up), 1 fixed (misread corrected), **6 unresolved** → review
  (was 159). Tesseract was needed for only **8** of 188.
- ~1 s/page OCR (Vision); 6-page statements take 5–14 s including re-reads.

## Parser-only (no retry), Vision, 29 statements
| Dataset | Row recall | Balance | Description |
|---|---|---|---|
| Synthetic scans (9) | 84.6% | 95.0% | 55.5% |
| AgamiAI Scanned Type1 (10) | 98.1% | 97.9% | 86.2% |
| AgamiAI Scanned Type2 (10) | 95.6% | 100% | 91.6% |

## Fixed during 1b (found by evals)
- **Sheared scans**: AgamiAI scans have level-shifted rows but upright columns (shear, not
  rotation). Rotating fixed rows but tilted the column borders → borders lost, columns merged,
  whole pages of rows lost (t1_00003 130/159, t2_00003 126/163). Now: rotate, then measure the
  vertical borders' lean and shear them upright → 159/159 and 161/163.
- **Retry cascade** (user design): Vision 2× → Tesseract only on disagreement → majority vote.
- Header row split across two OCR lines at 150 DPI → header judged over a ±6pt band.
- OCR boxes touching ("Amount|Dr/Cr", "Deposit Amt.|Closing Balance") → header words are split
  where the table body shows a whitespace channel; Dr/Cr always its own column; suffix words
  ("No.", "Code", "Amt.") attach to their title.
- Faint/broken column borders → border = total segment length under the header; a ruled cell
  holding two different titled roles is split at the body gap.
- OCR amount slips: '48.551.19', '1,52.482.56', 'Cг', 'O' for 0 → normalised in `parse_amount`.
- IFSC 'ICICOSF0002' → 'ICIC0SF0002' (5th char of an IFSC is always 0).
- White-on-blue header borders invisible after thresholding → body borders just under the header used.

## Known gaps
- Synthetic poor-quality scans (150 DPI, heavy blur/noise): ~7% of rows missed and weak
  descriptions — a harsher test than the AgamiAI scans; no vertical borders to anchor columns.
- Descriptions: OCR character errors (dropped punctuation, O/0) — don't affect reconciliation.
- OCR bake-off (Vision vs Tesseract vs RapidOCR) — deferred by decision.
