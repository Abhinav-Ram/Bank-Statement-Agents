# Phase 1b spec: scanned statements

**Exit criterion:** scans reach a measured reconciliation rate; failures stop at the review gate.

## Data
| Set | Statements | Notes |
|---|---|---|
| AgamiAI Scanned Type1 / Type2 | 100 + 100 | 300-DPI JPEG per page (2481×3508), clean flatbed-style, faint watermark; image-only copies of the digital set, same JSON truth |
| Synthetic scans | 9 | Our 3 layouts, degraded at good / medium / poor (tilt up to ±2.2°, blur, noise, uneven light, JPEG) |

Scan type answer (open item 3): AgamiAI is flatbed-style; phone photos are out of scope for v1.

## Pipeline additions
```
detect (per page: text layer ≥ 50 chars → digital, else scanned)
scanned page → preprocess → OCR → words + ruling lines → same table reconstruction as digital
```
- **preprocess** (`app/ocr/preprocess.py`, OpenCV): embedded image at native DPI (or render at
  300 DPI) → grayscale → projection-profile deskew (±4°, 0.1° steps) → median denoise.
  Coordinates normalised to A4 points so digital layout thresholds apply unchanged.
- **ruling lines**: adaptive threshold + morphological open with long horizontal / vertical
  kernels → `Rules`. Light borders on dark header fills are lost; body borders just under the
  header are used instead.
- **OCR** (`app/ocr/engines.py`): one interface, three engines — Apple Vision (word boxes via
  `boundingBoxForRange`, language correction off), Tesseract 5 (`--psm 11`), RapidOCR (PaddleOCR
  models on ONNX; line boxes split into words). Full PaddleOCR is skipped on 8 GB; RapidOCR runs
  the same model family.
- Rows from OCR carry `source="ocr"`: a running-balance break on them is **retryable**
  (unlike a clean digital row, where it is a source inconsistency).

## Scan retry (validate → retry, max 2)
For each break at row *i* (and row *i−1*, since one misread balance breaks two rows):
re-OCR the row strip at 2× with the primary engine and with the second engine, parse the cells
with the page's column layout, and pick the candidate values that close the chain.
If every engine reads the same printed values, the break is reclassified as
`source_inconsistency` (the scan matches the paper; it doesn't add up). Unresolved → review gate.

## Metrics
Same as 1a (row recall/precision, field accuracy, exact rows, reconciliation pass vs truth's own
pass, metadata), per OCR engine and per scan quality, plus seconds per page.

## Guardrail tests
Deskew recovers a known rotation; ruling-line detection finds drawn lines; detect routes text vs
image pages; OCR words come back in normalised points; a lone sign/currency token never crashes
amount parsing; retry picks the candidate that closes the balance chain and never invents values.
