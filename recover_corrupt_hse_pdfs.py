r"""Try to recover HSE PDFs whose `text_extraction_status` is 'failed'/'empty'.

Strategy. For each corrupt row (all hse_wayback) we extract the *original*
URL from the source_url's `id_/` payload form, query the Wayback CDX API for
every successful PDF capture of that URL, and try alternate timestamps until
PyMuPDF can parse one. On success we overwrite the local file, refresh
sha256/bytes/source_url, clear the failure status, and re-run extract_and_index
so paragraphs + embeddings get rebuilt.

Why this works. The corrupt cluster sits at byte sizes 178k/180k/401-404k
suggesting Wayback served truncated chunks (e.g. all five May/Apr 2023 PDFs
were captured on 2023-10-08 within a 30-min window — same crawler hiccup).
Other timestamps for the same URL frequently have a different `digest` and
parse cleanly.

Defaults to dry-run. With `--apply` it overwrites files in place.

    .\.venv\Scripts\python.exe recover_corrupt_hse_pdfs.py
    .\.venv\Scripts\python.exe recover_corrupt_hse_pdfs.py --apply
    .\.venv\Scripts\python.exe recover_corrupt_hse_pdfs.py --apply --max-snapshots 8

Note: `text_extraction_status='empty'` rows (e.g. the 2013 Ciaran Lynch PDF
with `pages=0`) are also tried — but if the underlying file is malformed
across every snapshot, no timestamp will help.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pq_tracker import config as cfg  # noqa: E402
from pq_tracker import db  # noqa: E402
from pq_tracker import hse_scraper as scraper  # noqa: E402
from pq_tracker import hse_text  # noqa: E402

log = logging.getLogger("recover")

# Cap how many alternate snapshots we try per PDF. CDX can return dozens for
# popular URLs; trying them all is rude to the IA and rarely needed.
DEFAULT_MAX_SNAPSHOTS = 6
CDX_URL = "https://web.archive.org/cdx/search/cdx"


def _original_url(wayback_src: str) -> str | None:
    """Extract the original URL from a wayback id_ replay URL."""
    marker = "id_/"
    i = wayback_src.find(marker)
    if i < 0:
        return None
    return wayback_src[i + len(marker):]


def _cdx_snapshots(session, original: str, *, delay_s: float) -> list[dict]:
    """Return all PDF captures of `original` as list of {timestamp, digest, length, status}."""
    params = {
        "url": original,
        "output": "json",
        "filter": ["mimetype:application/pdf", "statuscode:200"],
    }
    qs = urllib.parse.urlencode([(k, v) for k, vs in params.items()
                                 for v in (vs if isinstance(vs, list) else [vs])])
    url = f"{CDX_URL}?{qs}"
    r = scraper._polite_get(session, url, delay_s=delay_s, max_retries=4)
    r.raise_for_status()
    rows = r.json() if r.text else []
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    idx = {c: i for i, c in enumerate(header)}
    out: list[dict] = []
    for row in data:
        try:
            out.append({
                "timestamp": row[idx["timestamp"]],
                "digest": row[idx["digest"]],
                "length": int(row[idx["length"]]) if row[idx.get("length", -1)] else 0,
                "status": row[idx["statuscode"]],
            })
        except (IndexError, KeyError, ValueError):
            continue
    return out


def _build_replay_url(original: str, ts: str) -> str:
    return f"https://web.archive.org/web/{ts}id_/{original}"


def _attempt_parse(local_path: Path) -> tuple[bool, int]:
    """Returns (parses_with_pages, page_count)."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(str(local_path)) as doc:
            n = doc.page_count
            return n > 0, n
    except Exception as e:
        log.debug("parse failed for %s: %s", local_path, e)
        return False, 0


def _process_one(conn, session, row: sqlite3.Row, *, max_snapshots: int,
                 delay_s: float, apply: bool) -> dict:
    """Try to recover a single corrupt PDF row. Returns outcome dict."""
    pdf_id = row["id"]
    src_url = row["source_url"]
    orig = _original_url(src_url)
    if not orig:
        return {"id": pdf_id, "outcome": "no_original_url"}

    log.info("[%d] original: %s", pdf_id, orig)
    snapshots = _cdx_snapshots(session, orig, delay_s=delay_s)
    if not snapshots:
        return {"id": pdf_id, "outcome": "no_cdx_rows"}

    # Group by digest — same digest = same content, no point trying twice.
    by_digest: dict[str, dict] = {}
    for s in snapshots:
        by_digest.setdefault(s["digest"], s)
    distinct = list(by_digest.values())
    log.info("[%d] CDX returned %d captures, %d distinct digests",
             pdf_id, len(snapshots), len(distinct))

    # Heuristic: try larger payloads first (corrupt ones cluster at small sizes).
    # Within ties, prefer the most recent timestamp.
    distinct.sort(key=lambda s: (-s["length"], s["timestamp"]), reverse=False)
    distinct.sort(key=lambda s: (-s["length"], -int(s["timestamp"])))
    # ^ sort by (largest length, newest timestamp)

    candidates = distinct[:max_snapshots]
    target = cfg.ROOT / row["local_path"] if row["local_path"] else None

    for cand in candidates:
        ts = cand["timestamp"]
        url = _build_replay_url(orig, ts)
        log.info("[%d]   try ts=%s digest=%s length=%d",
                 pdf_id, ts, cand["digest"][:12], cand["length"])
        # Download to a probe file so we don't clobber the existing one until success.
        probe = target.with_suffix(target.suffix + ".probe") if target else \
                Path(f"_probe_pdf_{pdf_id}_{ts}.pdf")
        try:
            sha, n_bytes = scraper.download_pdf(session, url, probe, delay_s=delay_s)
        except Exception as e:
            log.warning("[%d]     download failed: %s", pdf_id, e)
            continue
        ok, pages = _attempt_parse(probe)
        if not ok:
            log.info("[%d]     parsed pages=%d (no good)", pdf_id, pages)
            try:
                probe.unlink()
            except OSError:
                pass
            continue
        log.info("[%d]     PARSED OK: %d pages, sha=%s, %d bytes",
                 pdf_id, pages, sha[:12], n_bytes)
        if not apply:
            try:
                probe.unlink()
            except OSError:
                pass
            return {"id": pdf_id, "outcome": "would_recover",
                    "pages": pages, "sha256": sha, "bytes": n_bytes,
                    "from_ts": ts}

        # APPLY: replace local file, update DB, re-extract.
        if target and target.exists():
            try:
                target.unlink()
            except OSError as e:
                log.warning("[%d]     could not remove old file: %s", pdf_id, e)
        probe.replace(target)
        # Update source_url to the new replay URL too — the old timestamp's
        # payload was bad, the new one is what we now have on disk. Keep
        # source_url unique by checking first.
        existing = conn.execute(
            "SELECT id FROM hse_pdfs WHERE source_url = ? AND id <> ?",
            (url, pdf_id),
        ).fetchone()
        if not existing:
            conn.execute(
                "UPDATE hse_pdfs SET source_url = ?, sha256 = ?, bytes = ?, "
                "  text_extraction_status = NULL, text_extracted_at = NULL, "
                "  text_page_count = NULL, ocr_used = 0 WHERE id = ?",
                (url, sha, n_bytes, pdf_id),
            )
        else:
            # An identical replay URL is already in the DB (unusual). Just
            # refresh sha/bytes/status without changing source_url.
            conn.execute(
                "UPDATE hse_pdfs SET sha256 = ?, bytes = ?, "
                "  text_extraction_status = NULL, text_extracted_at = NULL, "
                "  text_page_count = NULL, ocr_used = 0 WHERE id = ?",
                (sha, n_bytes, pdf_id),
            )
        # Re-extract — paragraphs + embeddings will populate.
        try:
            result = hse_text.extract_and_index(conn, pdf_id, root=cfg.ROOT, force=True)
            log.info("[%d]     extract_and_index: status=%s paragraphs=%d emb=%d",
                     pdf_id, result["status"], result["paragraphs"],
                     result["embeddings_inserted"])
            conn.commit()
            return {"id": pdf_id, "outcome": "recovered",
                    "pages": pages, "sha256": sha, "bytes": n_bytes,
                    "from_ts": ts, "extract_status": result["status"],
                    "paragraphs": result["paragraphs"]}
        except Exception as e:
            log.error("[%d]     extract_and_index failed: %s", pdf_id, e)
            conn.commit()  # at least keep the new sha/bytes saved
            return {"id": pdf_id, "outcome": "downloaded_but_extract_failed",
                    "error": str(e)}

    return {"id": pdf_id, "outcome": "no_working_snapshot",
            "tried": len(candidates)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="commit (default is dry-run, only probes downloads)")
    parser.add_argument("--max-snapshots", type=int, default=DEFAULT_MAX_SNAPSHOTS,
                        help="cap candidates per corrupt PDF (default: %d)" % DEFAULT_MAX_SNAPSHOTS)
    parser.add_argument("--delay-s", type=float, default=2.0,
                        help="seconds between Wayback requests (default: 2.0)")
    parser.add_argument("--ids", type=str, default=None,
                        help="comma-separated hse_pdfs.id list to limit to (default: all corrupt)")
    args = parser.parse_args()

    cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = cfg.LOGS_DIR / f"recover-corrupt-{time.strftime('%Y%m%d-%H%M%S')}.log"
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(log_path, encoding="utf-8")],
                        force=True)

    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        if args.ids:
            id_list = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
            placeholders = ",".join("?" * len(id_list))
            rows = conn.execute(
                f"SELECT id, source_url, local_path, text_extraction_status, "
                f"  bytes, pq_refs_json FROM hse_pdfs WHERE id IN ({placeholders}) "
                f"ORDER BY id", id_list,
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, source_url, local_path, text_extraction_status,
                          bytes, pq_refs_json FROM hse_pdfs
                    WHERE text_extraction_status IN ('failed','empty')
                    ORDER BY pq_refs_json"""
            ).fetchall()

        log.info("=== recovery start: %d corrupt PDFs (apply=%s) ===",
                 len(rows), args.apply)
        log.info("log: %s", log_path)

        session = scraper.make_session()
        outcomes: list[dict] = []
        for r in rows:
            log.info("--- pdf_id=%d status=%s pqs=%s ---",
                     r["id"], r["text_extraction_status"], r["pq_refs_json"])
            try:
                out = _process_one(conn, session, r,
                                   max_snapshots=args.max_snapshots,
                                   delay_s=args.delay_s,
                                   apply=args.apply)
            except Exception as e:
                log.error("[%d] unhandled: %s", r["id"], e)
                out = {"id": r["id"], "outcome": "exception", "error": str(e)}
            outcomes.append(out)

    # Summary
    print("\n=== summary ===")
    by_outcome: dict[str, int] = {}
    for o in outcomes:
        by_outcome[o["outcome"]] = by_outcome.get(o["outcome"], 0) + 1
    for k in sorted(by_outcome):
        print(f"  {k}: {by_outcome[k]}")
    if not args.apply:
        print("\n(dry-run — re-run with --apply to actually replace files)")
    print(f"\nlog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
