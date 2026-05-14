"""Scrape HSE Parliamentary Question response PDFs.

Two sources:
  - Live HSE site (about.hse.ie, Nov 2025 onwards): paginated listing
    at /publications/?type=Responses+to+parliamentary+questions, with each
    entry linking to a per-publication page whose body contains the PDF URL.
  - Wayback Machine archive of the old www.hse.ie/eng/about/personalpq/pq/
    tree (2020 onwards, possibly earlier). We use the CDX API to enumerate
    every archived PDF in one query.

A PDF can answer multiple grouped PQs; we track those as a many-to-many
junction in hse_pdf_pqs.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import time
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

import requests

log = logging.getLogger(__name__)

USER_AGENT = "pq-tracker/1.0 (local research tool)"

LIVE_LISTING_URL = (
    "https://about.hse.ie/publications/"
    "?type=Responses+to+parliamentary+questions&page={page}"
)
WB_CDX_URL = (
    "https://web.archive.org/cdx/search/cdx"
    "?url=hse.ie/eng/about/personalpq/pq/*"
    "&filter=mimetype:application/pdf"
    "&filter=statuscode:200"
    "&collapse=urlkey"
    "&output=json"
)

# Matches "12345-26" or "12345/26" as a contiguous PQ ref. Uses negative
# lookarounds so refs separated only by underscores (PQ_25732-26__28456-26_...)
# still match — \b fails there because _ is a word char.
_REF_RE = re.compile(r"(?<!\d)(\d{1,6})[-/](\d{2})(?!\d)")

# Wayback grouped format: "pqs-NN-NN-NN-YY" where one year suffix applies to
# every preceding number (e.g. billy-kelleher-pqs-16300-16302-16317-14-.pdf).
# Requires an explicit pq/pqs prefix so we don't capture random digit runs from
# unrelated parts of the filename.
_WB_GROUP_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])pqs?[-_](\d{2,6}(?:-\d{2,6})+)-(\d{2})(?!\d)",
    re.IGNORECASE,
)

_LIVE_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="(/publications/[^"]*?pq-\d{1,6}-\d{2}/?)"[^>]*>',
    re.IGNORECASE,
)
_LIVE_PDF_RE = re.compile(
    r'href="(https?://about\.hse\.ie/api/v2/download-file/[^"]+\.pdf)"',
    re.IGNORECASE,
)
# The publication page on about.hse.ie carries two month-precision dates inside
# stable class names: "Published: <Month YYYY>" (note the typo "pubished" — kept
# server-side, looks intentional) and "Updated: <Month YYYY>". We treat Published
# as the publication_date for sorting; if absent we fall back to Updated.
_LIVE_PUBDATE_RE = re.compile(
    r'Published:\s*([A-Z][a-z]+\s+\d{4})', re.IGNORECASE,
)
_LIVE_UPDATE_RE = re.compile(
    r'Updated:\s*([A-Z][a-z]+\s+\d{4})', re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["january","february","march","april","may","june",
     "july","august","september","october","november","december"], start=1)}


def _parse_month_year(s: str) -> Optional[date]:
    """'September 2025' → date(2025, 9, 1). Month precision only — HSE doesn't
    publish the day on the listing page. Returning the 1st keeps ISO-sortability.
    """
    if not s:
        return None
    parts = s.strip().split()
    if len(parts) != 2:
        return None
    m = _MONTHS.get(parts[0].lower())
    try:
        y = int(parts[1])
    except ValueError:
        return None
    if not m:
        return None
    return date(y, m, 1)


@dataclasses.dataclass
class ScrapedPdf:
    source: str            # 'hse_live' or 'hse_wayback'
    source_url: str        # canonical fetch URL (Wayback id_ form for archived items)
    index_url: Optional[str]  # the listing/day page that referenced this PDF
    filename: str
    pq_refs: list[str]     # normalized "12345/26" form
    publication_date: Optional[date]


def normalize_pq_ref(s: str) -> Optional[str]:
    """Pull a single canonical 'num/yy' PQ ref out of a string."""
    m = _REF_RE.search(s)
    if not m:
        return None
    return f"{int(m.group(1))}/{m.group(2)}"


def pq_refs_from_text(s: str) -> list[str]:
    """Extract every canonical 'num/yy' ref from a string, deduped, in order.

    Handles two distinct grouping styles seen in HSE filenames:
      - live HSE: 'PQ_25732-26__28456-26_-_Name.pdf'  (each ref has its own year)
      - Wayback:  'name-pqs-16300-16302-16317-14-.pdf'  (shared trailing year)
    """
    s = s or ""
    seen: set[str] = set()
    out: list[str] = []
    # Pass 1: shared-year groups (pqs-N-N-N-YY).
    for m in _WB_GROUP_RE.finditer(s):
        nums = m.group(1).split("-")
        yr = m.group(2)
        for n in nums:
            ref = f"{int(n)}/{yr}"
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    # Pass 2: standard num-yr pairs (handles live format and single-ref names).
    for m in _REF_RE.finditer(s):
        ref = f"{int(m.group(1))}/{m.group(2)}"
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _polite_get(session: requests.Session, url: str, *, delay_s: float,
                max_retries: int = 5, stream: bool = False) -> requests.Response:
    """GET with a fixed post-success delay and exponential backoff on 429/5xx."""
    attempt = 0
    while True:
        try:
            r = session.get(url, timeout=90, stream=stream)
        except requests.RequestException as e:
            if attempt >= max_retries:
                raise
            wait = min(60.0, (2 ** attempt) * delay_s)
            log.warning("network error for %s: %s (retry in %.1fs)", url, e, wait)
            time.sleep(wait)
            attempt += 1
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt >= max_retries:
                r.raise_for_status()
            wait = min(120.0, (2 ** attempt) * max(delay_s, 2.0))
            log.warning("got HTTP %d for %s (retry in %.1fs)", r.status_code, url, wait)
            r.close()
            time.sleep(wait)
            attempt += 1
            continue
        # Success-ish (200, 3xx, 404). Apply throttle, return.
        if not stream:
            time.sleep(delay_s)
        return r


# Live site walker

def iter_live_listing_pages(session: requests.Session, *, start_page: int = 1,
                            stop_page: Optional[int] = None,
                            delay_s: float = 1.0) -> Iterator[str]:
    """Yield absolute /publications/...-pq-N-YY/ URLs from successive listing pages.

    Stops on: empty page, HTTP non-200, or stop_page reached.
    """
    page = start_page
    seen: set[str] = set()
    while True:
        url = LIVE_LISTING_URL.format(page=page)
        log.info("live listing page %d", page)
        r = _polite_get(session, url, delay_s=delay_s)
        if r.status_code != 200:
            log.info("listing page %d returned HTTP %d, stopping", page, r.status_code)
            break
        hrefs = _LIVE_ANCHOR_RE.findall(r.text)
        if not hrefs:
            log.info("no PQ entries on page %d, stopping", page)
            break
        for href in hrefs:
            full = urllib.parse.urljoin("https://about.hse.ie/", href)
            if full in seen:
                continue
            seen.add(full)
            yield full
        if stop_page is not None and page >= stop_page:
            break
        page += 1


def fetch_live_publication(session: requests.Session, pub_url: str,
                           *, delay_s: float = 1.0) -> Optional[ScrapedPdf]:
    """Visit a /publications/...-pq-N-YY/ page and extract its PDF link."""
    r = _polite_get(session, pub_url, delay_s=delay_s)
    if r.status_code != 200:
        log.warning("publication %s returned HTTP %d", pub_url, r.status_code)
        return None
    m = _LIVE_PDF_RE.search(r.text)
    if not m:
        log.warning("no PDF link found on %s", pub_url)
        return None
    pdf_url = m.group(1)
    fname = urllib.parse.unquote(pdf_url.rsplit("/", 1)[-1])
    # The filename usually has all grouped refs, e.g. PQ_25732-26__28456-26_...pdf.
    # Fall back to the URL slug if the filename has no detectable ref.
    refs = pq_refs_from_text(fname) or pq_refs_from_text(pub_url)
    pub_m = _LIVE_PUBDATE_RE.search(r.text)
    upd_m = _LIVE_UPDATE_RE.search(r.text)
    pub_d = _parse_month_year(pub_m.group(1)) if pub_m else None
    if pub_d is None and upd_m:
        pub_d = _parse_month_year(upd_m.group(1))
    return ScrapedPdf(
        source="hse_live",
        source_url=pdf_url,
        index_url=pub_url,
        filename=fname,
        pq_refs=refs,
        publication_date=pub_d,
    )


# Wayback CDX walker

def _wb_ts_to_date(ts: str) -> Optional[date]:
    if not ts or len(ts) < 8:
        return None
    try:
        return date(int(ts[0:4]), int(ts[4:6]), int(ts[6:8]))
    except (ValueError, TypeError):
        return None


def iter_wayback_pdfs(session: requests.Session, *, delay_s: float = 5.0,
                      max_items: Optional[int] = None) -> Iterator[ScrapedPdf]:
    """Query the Wayback CDX API once, then yield one ScrapedPdf per unique PDF.

    Uses the 'id_' replay form so downloads return the original payload bytes,
    not the Wayback HTML wrapper. The CDX 'collapse=urlkey' option already
    deduplicates by URL — we pick the capture row CDX returned (most recent
    within their default rules).
    """
    log.info("querying Wayback CDX for HSE PQ PDFs (this can take ~60s)...")
    r = _polite_get(session, WB_CDX_URL, delay_s=delay_s, max_retries=6)
    r.raise_for_status()
    rows = r.json() if r.text else []
    if not rows:
        log.info("CDX returned no rows")
        return
    header, data = rows[0], rows[1:]
    idx = {c: i for i, c in enumerate(header)}
    log.info("CDX returned %d candidate PDFs", len(data))
    seen_urls: set[str] = set()
    count = 0
    for row in data:
        try:
            ts = row[idx["timestamp"]]
            orig = row[idx["original"]]
        except (IndexError, KeyError):
            continue
        if not orig.lower().endswith(".pdf"):
            continue
        # Construct a Wayback "raw payload" URL.
        src_url = f"https://web.archive.org/web/{ts}id_/{orig}"
        if src_url in seen_urls:
            continue
        seen_urls.add(src_url)
        fname = urllib.parse.unquote(orig.rsplit("/", 1)[-1])
        refs = pq_refs_from_text(fname)
        yield ScrapedPdf(
            source="hse_wayback",
            source_url=src_url,
            index_url=None,
            filename=fname,
            pq_refs=refs,
            publication_date=_wb_ts_to_date(ts),
        )
        count += 1
        if max_items is not None and count >= max_items:
            break


# Download + storage

def storage_path(root: Path, item: ScrapedPdf) -> Path:
    """Local path for a PDF. Layout: hse_pdfs/{live|wayback}/{yy}/{safe_filename}."""
    sub = "live" if item.source == "hse_live" else "wayback"
    yr = item.pq_refs[0].split("/")[-1] if item.pq_refs else "unknown"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", item.filename) or "unknown.pdf"
    return root / sub / yr / safe


def download_pdf(session: requests.Session, url: str, target: Path,
                 *, delay_s: float = 1.0) -> tuple[str, int]:
    """Stream a PDF to disk, compute sha256, return (hex_digest, bytes).

    Writes via a .part temp file so an interrupted download doesn't leave a
    corrupt file matching the final path. Throttles after success.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    h = hashlib.sha256()
    total = 0
    with _polite_get(session, url, delay_s=0.0, stream=True) as r:
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                h.update(chunk)
                total += len(chunk)
        if total == 0:
            raise RuntimeError(f"empty body from {url} (content-type={ctype})")
    tmp.replace(target)
    time.sleep(delay_s)
    return h.hexdigest(), total
