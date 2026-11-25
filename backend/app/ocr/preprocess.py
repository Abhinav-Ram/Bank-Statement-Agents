"""Scanned-page preprocessing (OpenCV): render → grayscale → deskew → denoise, plus ruling-line
detection so the table parser can use cell borders exactly as it does for digital PDFs.

Coordinates: everything downstream works in *page points normalised to A4 width* (595 portrait /
842 landscape), so the layout thresholds tuned on digital PDFs hold for scans of any DPI.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf

from app.parsing.layout import Rules


@dataclass
class PageImage:
    gray: np.ndarray  # uint8, deskewed + denoised
    scale: float  # image px → normalised points
    angle: float  # degrees rotated to deskew
    page_no: int
    shear: float = 0.0  # degrees of horizontal shear removed after rotation (sheared scans)

    def to_pt(self, v: float) -> float:
        return v * self.scale


def render_page(page: pymupdf.Page, dpi: int = 300) -> np.ndarray:
    """Grayscale image of the page. Image-only pages use the embedded image at native size."""
    imgs = page.get_images(full=True)
    if len(imgs) == 1:
        info = page.parent.extract_image(imgs[0][0])
        arr = np.frombuffer(info["image"], np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is not None and img.shape[1] >= 1200:
            return img
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy()


def estimate_skew(gray: np.ndarray, max_deg: float = 4.0, step: float = 0.1) -> float:
    """Projection-profile deskew: the angle that makes text rows sharpest (max row-sum variance)."""
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    h, w = bw.shape
    centre = (w / 2, h / 2)
    best, best_score = 0.0, -1.0
    for a in np.arange(-max_deg, max_deg + 1e-9, step):
        m = cv2.getRotationMatrix2D(centre, a, 1.0)
        rot = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        score = float(np.var(rot.sum(axis=1)))
        if score > best_score:
            best, best_score = float(a), score
    return round(best, 2)


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def vertical_tilt(gray: np.ndarray, min_lines: int = 4) -> float | None:
    """Signed lean (degrees from vertical) of the page's vertical ruling lines, or None if the
    page has too few of them to tell."""
    h, _w = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 8)
    m = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT,
                                                                      (3, max(20, h // 30))))
    lines = cv2.HoughLinesP(m, 1, np.pi / 3600, 150, minLineLength=h // 8, maxLineGap=15)
    if lines is None:
        return None
    seg = np.array(lines).reshape(-1, 4)
    ang = np.degrees(np.arctan2(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1]))
    ang = (ang + 90) % 180 - 90  # endpoint order is arbitrary → fold into [-90, 90)
    ang = ang[np.abs(ang) < 5]
    return float(np.median(ang)) if len(ang) >= min_lines else None


def unshear(gray: np.ndarray, lean_deg: float) -> np.ndarray:
    """Horizontal shear that makes leaning verticals vertical; horizontal lines stay horizontal."""
    h, w = gray.shape
    k = np.tan(np.radians(lean_deg))
    m = np.float32([[1, -k, k * h / 2], [0, 1, 0]])
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def preprocess(page: pymupdf.Page, page_no: int) -> PageImage:
    """Rotate so text rows are level; then, if the vertical borders now lean (the scan was
    sheared rather than rotated), shear them upright too."""
    gray = render_page(page)
    angle = estimate_skew(gray)
    gray = rotate(gray, angle)
    gray = cv2.medianBlur(gray, 3)  # speckle / scanner noise; keeps strokes
    shear = 0.0
    lean = vertical_tilt(gray) if abs(angle) >= 0.05 else None
    if lean is not None and abs(lean) >= 0.15:
        gray, shear = unshear(gray, lean), round(lean, 2)
    h, w = gray.shape
    target_w = 842.0 if w > h else 595.0
    return PageImage(gray, target_w / w, angle, page_no, shear)


def detect_rules(img: PageImage) -> Rules:
    """Horizontal / vertical ruling lines via morphology on an adaptive-threshold image."""
    g = img.gray
    h, w = g.shape
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 12)
    out = Rules()
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 25), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 60)))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk)
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    for mask, kind in ((horiz, "h"), (vert, "v")):
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for x, y, cw, ch, _area in stats[1:n]:
            if kind == "h" and cw > w * 0.04 and ch < h * 0.01:
                out.horizontal.append((img.to_pt(y + ch / 2), img.to_pt(x), img.to_pt(x + cw)))
            elif kind == "v" and ch > h * 0.008 and cw < w * 0.01:
                out.vertical.append((img.to_pt(x + cw / 2), img.to_pt(y), img.to_pt(y + ch)))
    return out
