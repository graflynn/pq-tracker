"""Extract paragraph text from HSE PQ-response PDFs, index it for search.

Two indexes are built per PDF:
  - lexical: rows in `hse_paragraphs` mirrored to FTS5 (`hse_paragraphs_fts`)
  - semantic: BGE-small vectors in `embeddings` table, keyed on
    (source_type='hse_paragraph', source_pdf_id, chunk_index=para_index)

PDF text extraction uses PyMuPDF (fitz) — fast and layout-aware on born-digital
PDFs. About 1 in 5 Wayback PDFs are image scans of typed HSE letters; for those
we fall back to Tesseract OCR via PyMuPDF's `page.get_textpage_ocr()`. If
Tesseract isn't installed, scans are marked `text_extraction_status='empty'`.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


_TESSERACT_DIR: Optional[str] = None
_TESSERACT_CHECKED = False


def _init_tesseract() -> bool:
    """Locate Tesseract and prep the process environment for PyMuPDF.

    Checks `tesseract` on PATH, then the standard Windows install at
    `C:\\Program Files\\Tesseract-OCR\\`. On success, prepends Tesseract's
    directory to PATH and sets `TESSDATA_PREFIX` so `Page.get_textpage_ocr()`
    works. Cached per-process. Returns True if Tesseract is available.
    """
    global _TESSERACT_DIR, _TESSERACT_CHECKED
    if _TESSERACT_CHECKED:
        return _TESSERACT_DIR is not None
    _TESSERACT_CHECKED = True

    on_path = shutil.which("tesseract")
    if on_path:
        _TESSERACT_DIR = str(Path(on_path).parent)
        os.environ.setdefault("TESSDATA_PREFIX", str(Path(_TESSERACT_DIR) / "tessdata"))
        log.info("Tesseract on PATH at %s", _TESSERACT_DIR)
        return True

    win_dir = Path(r"C:\Program Files\Tesseract-OCR")
    if (win_dir / "tesseract.exe").exists():
        _TESSERACT_DIR = str(win_dir)
        os.environ["PATH"] = str(win_dir) + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("TESSDATA_PREFIX", str(win_dir / "tessdata"))
        log.info("Tesseract found at %s (added to PATH)", win_dir)
        return True

    log.info("Tesseract not found — image-scan PDFs will be marked 'empty' "
             "(install Tesseract OCR to enable fallback)")
    return False


def tesseract_available() -> bool:
    """True if Tesseract is installed and PyMuPDF can drive it for OCR."""
    return _init_tesseract()

# Paragraph filtering thresholds. HSE PQ responses are typically a short
# preamble + a few content paragraphs. Anything under 30 chars is usually a
# page number, header fragment, or table cell that escaped layout grouping.
MIN_PARAGRAPH_CHARS = 30
# Above this, we re-split the paragraph along sentence boundaries so semantic
# chunks stay within the BGE-small 512-token window (~1500 chars empirically).
MAX_PARAGRAPH_CHARS = 1800

# Lines that look like page-number footers ("Page 3", "3 of 12", or just "3").
_PAGE_NUM_RE = re.compile(r"^\s*(page\s+)?\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)
# Run-on whitespace inside an extracted paragraph (incl. soft-wrapped lines).
_WS_RE = re.compile(r"\s+")


def _collect_header_footer_lines(pages_lines: list[list[str]]) -> set[str]:
    """Identify lines that appear at the top or bottom of most pages.

    Strategy: take the first non-empty line + last non-empty line of each
    page; if a line shows up on >=40% of pages with ≥3 occurrences, treat
    it as boilerplate and strip from every page.
    """
    if len(pages_lines) < 3:
        return set()
    candidates: Counter[str] = Counter()
    for lines in pages_lines:
        non_empty = [ln.strip() for ln in lines if ln.strip()]
        if not non_empty:
            continue
        # Header: first 1-2 lines. Footer: last 1-2 lines.
        for ln in non_empty[:2] + non_empty[-2:]:
            if 3 <= len(ln) <= 120:
                candidates[ln] += 1
    threshold = max(3, int(len(pages_lines) * 0.4))
    return {ln for ln, n in candidates.items() if n >= threshold}


def _split_into_paragraphs(text: str) -> list[str]:
    """Split a single page's text into paragraph candidates by blank lines."""
    # Normalise: PyMuPDF's get_text() uses \n line breaks; paragraphs are
    # separated by one or more blank lines.
    parts = re.split(r"\n\s*\n+", text)
    out: list[str] = []
    for p in parts:
        # Within a paragraph, soft-wrapped lines should rejoin with a space.
        joined = _WS_RE.sub(" ", p).strip()
        if joined:
            out.append(joined)
    return out


def _sentence_split_long(text: str, max_chars: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    """Greedy sentence-boundary split for paragraphs longer than max_chars.

    Keeps chunks under the BGE-small token budget without slicing mid-sentence.
    Falls back to a hard char-split if no sentence boundary is in range.
    """
    if len(text) <= max_chars:
        return [text]
    # Sentence boundaries: ". ", "? ", "! " — not perfect, but good enough on
    # HSE response prose. Iteratively peel off the largest under-budget prefix.
    out: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        slice_end = remaining.rfind(". ", 0, max_chars)
        if slice_end < max_chars // 2:
            # No good sentence break — hard-split on a word boundary instead.
            slice_end = remaining.rfind(" ", 0, max_chars)
            if slice_end <= 0:
                slice_end = max_chars
        else:
            slice_end += 1  # keep the period
        out.append(remaining[:slice_end].strip())
        remaining = remaining[slice_end:].lstrip()
    if remaining:
        out.append(remaining)
    return [c for c in out if c]


def extract_paragraphs(local_path: Path, *, allow_ocr: bool = True
                       ) -> tuple[list[tuple[int, int, str]], int, bool]:
    """Extract (page_no, para_index, text) tuples from a local HSE PDF.

    Returns (paragraphs, page_count, ocr_used). page_no is 1-based.
    para_index is 0-based within the PDF (not per-page) so callers have a
    stable monotone key for the embeddings `chunk_index` column.

    When native text extraction yields nothing across all pages, and
    `allow_ocr=True` and Tesseract is available, the pages are re-rendered
    via Tesseract OCR. The result is the same tuple shape; `ocr_used=True`
    flags that OCR was applied (text quality is lower — header/logo regions
    can produce garbage tokens, but body prose is usable).

    Empty result list = no extractable text even after the OCR attempt.
    """
    import fitz  # PyMuPDF; imported lazily so non-extraction CLI calls stay light
    doc = fitz.open(local_path)
    try:
        pages_text: list[str] = []
        for page in doc:
            txt = page.get_text("text") or ""
            pages_text.append(txt)
        page_count = len(pages_text)
        if not pages_text:
            return [], 0, False

        ocr_used = False
        # OCR fallback: native extraction returned nothing across every page.
        if allow_ocr and not any(t.strip() for t in pages_text) and tesseract_available():
            log.info("native text empty across %d pages → trying OCR (%s)",
                     page_count, local_path.name)
            ocr_pages: list[str] = []
            for page in doc:
                try:
                    tp = page.get_textpage_ocr(language="eng", dpi=300, full=False)
                    ocr_pages.append(page.get_text("text", textpage=tp) or "")
                except Exception as e:
                    log.warning("OCR failed on page of %s: %s", local_path.name, e)
                    ocr_pages.append("")
            pages_text = ocr_pages
            ocr_used = True

        pages_lines = [t.splitlines() for t in pages_text]
        boilerplate = _collect_header_footer_lines(pages_lines)
        out: list[tuple[int, int, str]] = []
        para_idx = 0
        for page_no, txt in enumerate(pages_text, start=1):
            cleaned_lines = []
            for ln in txt.splitlines():
                stripped = ln.strip()
                if stripped in boilerplate:
                    continue
                if _PAGE_NUM_RE.match(stripped):
                    continue
                cleaned_lines.append(ln)
            cleaned = "\n".join(cleaned_lines)
            for paragraph in _split_into_paragraphs(cleaned):
                if len(paragraph) < MIN_PARAGRAPH_CHARS:
                    continue
                for piece in _sentence_split_long(paragraph):
                    if len(piece) < MIN_PARAGRAPH_CHARS:
                        continue
                    out.append((page_no, para_idx, piece))
                    para_idx += 1
        return out, page_count, ocr_used
    finally:
        doc.close()


def extract_and_index(conn: sqlite3.Connection, pdf_id: int, *,
                      root: Path, existing_para_count: int | None = None,
                      existing_emb_count: int | None = None,
                      force: bool = False) -> dict:
    """Extract paragraphs from one PDF and refresh both indexes.

    Idempotent: if `existing_para_count` matches the freshly-extracted count
    and embeddings are present, skip the work. Pass `force=True` to bypass.

    Returns {'pdf_id', 'status', 'paragraphs', 'embeddings_inserted',
              'page_count', 'skipped'}.
    """
    from . import db as db_mod
    from . import embeddings as emb_mod

    row = conn.execute(
        "SELECT id, local_path, text_extraction_status, text_page_count "
        "  FROM hse_pdfs WHERE id = ?", (pdf_id,)
    ).fetchone()
    if row is None:
        return {"pdf_id": pdf_id, "status": "missing_row", "paragraphs": 0,
                "embeddings_inserted": 0, "page_count": 0, "skipped": True,
                "ocr_used": False}
    if not row["local_path"]:
        return {"pdf_id": pdf_id, "status": "no_local_copy", "paragraphs": 0,
                "embeddings_inserted": 0, "page_count": 0, "skipped": True,
                "ocr_used": False}
    local = root / row["local_path"]
    if not local.exists():
        log.warning("hse_pdf id=%d local_path missing on disk: %s", pdf_id, local)
        db_mod.set_hse_extraction_status(conn, pdf_id, "failed", None)
        return {"pdf_id": pdf_id, "status": "failed", "paragraphs": 0,
                "embeddings_inserted": 0, "page_count": 0, "skipped": False,
                "ocr_used": False}

    try:
        paragraphs, page_count, ocr_used = extract_paragraphs(local)
    except Exception as e:
        log.warning("extract failed pdf_id=%d path=%s: %s", pdf_id, local, e)
        db_mod.set_hse_extraction_status(conn, pdf_id, "failed", None)
        return {"pdf_id": pdf_id, "status": "failed", "paragraphs": 0,
                "embeddings_inserted": 0, "page_count": 0, "skipped": False,
                "ocr_used": False}

    if not paragraphs:
        # No extractable text — even after OCR if it was attempted. Don't keep
        # any stale rows around.
        db_mod.replace_hse_paragraphs(conn, pdf_id, [])
        db_mod.set_hse_extraction_status(conn, pdf_id, "empty", page_count,
                                         ocr_used=ocr_used)
        return {"pdf_id": pdf_id, "status": "empty", "paragraphs": 0,
                "embeddings_inserted": 0, "page_count": page_count,
                "skipped": False, "ocr_used": ocr_used}

    # Idempotent fast-path: paragraph count unchanged AND embeddings already
    # match that count. We don't compare text content — embeddings are
    # deterministic from text and chunk re-creation is the slow part. If the
    # caller suspects content changed (e.g. re-download), pass force=True.
    if not force and existing_para_count == len(paragraphs):
        target_emb = len(paragraphs)
        if existing_emb_count is None:
            existing_emb_count = conn.execute(
                "SELECT COUNT(*) FROM embeddings "
                "  WHERE source_pdf_id = ? AND source_type = 'hse_paragraph' "
                "    AND model = ?",
                (pdf_id, emb_mod.MODEL_NAME),
            ).fetchone()[0]
        if existing_emb_count == target_emb:
            return {"pdf_id": pdf_id, "status": "done", "paragraphs": target_emb,
                    "embeddings_inserted": 0, "page_count": page_count, "skipped": True}

    # Write paragraphs (replaces any stale set; clears old embeddings).
    db_mod.replace_hse_paragraphs(conn, pdf_id, paragraphs)
    # Embed paragraph texts in one batch.
    texts = [p[2] for p in paragraphs]
    vecs = emb_mod.embed_texts(texts)
    now = datetime.utcnow().isoformat(timespec="seconds")
    rows = [("hse_paragraph", None, pdf_id, para_idx, text,
             emb_mod.MODEL_NAME, emb_mod.DIMS, emb_mod.pack(v), now)
            for (page_no, para_idx, text), v in zip(paragraphs, vecs)]
    emb_mod.insert_embeddings(conn, rows)
    db_mod.set_hse_extraction_status(conn, pdf_id, "done", page_count,
                                     ocr_used=ocr_used)
    return {"pdf_id": pdf_id, "status": "done", "paragraphs": len(paragraphs),
            "embeddings_inserted": len(rows), "page_count": page_count,
            "skipped": False, "ocr_used": ocr_used}


def iter_pending_pdf_ids(conn: sqlite3.Connection, *, matched_only: bool,
                         redo: bool = False, redo_empty: bool = False
                         ) -> Iterable[int]:
    """PDF ids worth processing now.

    Default: PDFs with a local copy and no `text_extraction_status='done'`
    (so 'NULL' or 'failed' qualify; 'done' and 'empty' are skipped).
    `redo_empty=True` also re-processes 'empty' rows — useful after enabling
    Tesseract to retry image-scan PDFs with OCR.
    `redo=True` returns every PDF with a local copy, even ones already 'done'.
    `matched_only=True` further restricts to PDFs whose junction has at least
    one row in `questions` (matches the download-priority policy).
    """
    base = """SELECT p.id
                FROM hse_pdfs p
               WHERE p.local_path IS NOT NULL"""
    if not redo:
        if redo_empty:
            base += (" AND (p.text_extraction_status IS NULL"
                     " OR p.text_extraction_status IN ('failed','empty'))")
        else:
            base += (" AND (p.text_extraction_status IS NULL"
                     " OR p.text_extraction_status = 'failed')")
    if matched_only:
        base += """ AND p.id IN (
                      SELECT DISTINCT j.hse_pdf_id
                        FROM hse_pdf_pqs j
                        JOIN questions q ON q.pq_ref = j.pq_ref
                    )"""
    base += " ORDER BY p.id"
    for r in conn.execute(base):
        yield r[0]
