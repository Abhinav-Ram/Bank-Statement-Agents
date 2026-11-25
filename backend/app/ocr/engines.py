"""OCR engines behind one interface: image → word boxes (in normalised page points).

  vision     Apple Vision (VNRecognizeTextRequest, accurate, no language correction) via pyobjc;
             word boxes from boundingBoxForRange. macOS only.
  tesseract  Tesseract 5 via pytesseract (word-level boxes natively).
  rapidocr   PaddleOCR detection+recognition models on ONNX Runtime (line boxes, split into words).
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache

import cv2
import numpy as np

from app.ocr.preprocess import PageImage
from app.parsing.layout import Word

ENGINES = ("vision", "tesseract", "rapidocr")


class OCRWord(Word):
    """A Word with recognition confidence (0..1)."""

    def __init__(self, x0, y0, x1, y1, text, conf=1.0):
        super().__init__(x0, y0, x1, y1, text)
        self.conf = conf


def ocr_page(img: PageImage, engine: str, region: tuple[int, int, int, int] | None = None,
             upscale: float = 1.0) -> list[OCRWord]:
    """OCR the page (or a pixel region x0,y0,x1,y1 of it). Boxes come back in page points."""
    g = img.gray
    ox = oy = 0
    if region:
        x0, y0, x1, y1 = region
        g = g[max(0, y0):y1, max(0, x0):x1]
        ox, oy = max(0, x0), max(0, y0)
    if upscale != 1.0:
        g = cv2.resize(g, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    raw = {"vision": _vision, "tesseract": _tesseract, "rapidocr": _rapid}[engine](g)
    s = img.scale
    return [OCRWord((x0 / upscale + ox) * s, (y0 / upscale + oy) * s,
                    (x1 / upscale + ox) * s, (y1 / upscale + oy) * s, t, c)
            for t, c, x0, y0, x1, y1 in raw if t.strip()]


# --------------------------------------------------------------------------- Apple Vision
def _vision(gray: np.ndarray) -> list[tuple]:
    if sys.platform != "darwin":
        raise RuntimeError("Apple Vision OCR needs macOS")
    import Quartz
    import Vision
    from Foundation import NSData

    h, w = gray.shape
    _ok, png = cv2.imencode(".png", gray)
    data = NSData.dataWithBytes_length_(png.tobytes(), len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)  # account numbers / refs must not be "corrected"
    req.setRecognitionLanguages_(["en-US"])
    req.setMinimumTextHeight_(0.0)
    handler.performRequests_error_([req], None)
    out = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)[0]
        text, conf = str(cand.string()), float(cand.confidence())
        for m in re.finditer(r"\S+", text):
            box, _err = cand.boundingBoxForRange_error_((m.start(), m.end() - m.start()), None)
            if box is None:
                continue
            bb = box.boundingBox()  # normalised, origin bottom-left
            x0 = bb.origin.x * w
            x1 = (bb.origin.x + bb.size.width) * w
            y1 = (1 - bb.origin.y) * h
            y0 = (1 - bb.origin.y - bb.size.height) * h
            out.append((m.group(), conf, x0, y0, x1, y1))
    del Quartz
    return out


# --------------------------------------------------------------------------- Tesseract
def _tesseract(gray: np.ndarray) -> list[tuple]:
    import pytesseract

    d = pytesseract.image_to_data(gray, config="--oem 1 --psm 11 -c preserve_interword_spaces=1",
                                  output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(d["text"]):
        conf = float(d["conf"][i])
        if t.strip() and conf >= 0:
            x, y, bw, bh = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            out.append((t, conf / 100, x, y, x + bw, y + bh))
    return out


# --------------------------------------------------------------------------- RapidOCR
@lru_cache(maxsize=1)
def _rapid_engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _rapid(gray: np.ndarray) -> list[tuple]:
    res, _ = _rapid_engine()(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
    out = []
    for box, text, score in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        # Line box → word boxes, apportioned by character count.
        n = max(1, len(text))
        for m in re.finditer(r"\S+", text):
            a = x0 + (x1 - x0) * m.start() / n
            b = x0 + (x1 - x0) * m.end() / n
            out.append((m.group(), float(score), a, y0, b, y1))
    return out
