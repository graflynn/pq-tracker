"""CLI entry point for HSE PDF scraping.

Subcommands:
  backfill          one-off full scrape of live + Wayback PDFs (resumable)
  ingest            incremental: walk live site page 1+ until we hit a known URL
  backfill-missing  targeted ?query= lookup for answered PQs with no HSE link
  stats             report counts of HSE PDFs / matched PQ refs
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config as cfg
from . import db
from . import hse_scraper as scraper
from . import hse_text
from .matching import answer_defers_to_hse

log = logging.getLogger(__name__)


def _configure_logging(prefix: str) -> Path:
    cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = cfg.LOGS_DIR / f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path


def _save(conn, item: scraper.ScrapedPdf, local_path: Path | None,
          sha256: str | None, bytes_size: int | None) -> int:
    pdf_id, inserted = db.upsert_hse_pdf(
        conn,
        source=item.source,
        source_url=item.source_url,
        index_url=item.index_url,
        filename=item.filename,
        pq_refs=item.pq_refs,
        publication_date=item.publication_date.isoformat() if item.publication_date else None,
        local_path=str(local_path.relative_to(cfg.ROOT)).replace("\\", "/") if local_path else None,
        sha256=sha256,
        bytes_size=bytes_size,
        fetched_at=datetime.utcnow().isoformat(timespec="seconds") if sha256 else None,
    )
    return pdf_id


def _is_pq_linked(conn, pq_refs: list[str]) -> bool:
    """True iff at least one of these refs is in the questions table."""
    if not pq_refs:
        return False
    placeholders = ",".join("?" * len(pq_refs))
    row = conn.execute(
        f"SELECT 1 FROM questions WHERE pq_ref IN ({placeholders}) LIMIT 1", pq_refs
    ).fetchone()
    return row is not None


def _extract_after_download(conn, pdf_id: int, pq_refs: list[str]) -> None:
    """Best-effort: extract text + embed paragraphs after a successful download.

    Restricted to PDFs that join a row in `questions` (matches the backfill
    policy — Wayback in particular catalogs thousands of unmatched PDFs we
    don't index). Failures are logged but don't fail the download.
    """
    if not _is_pq_linked(conn, pq_refs):
        return
    try:
        result = hse_text.extract_and_index(conn, pdf_id, root=cfg.ROOT)
        log.info("text-index pdf_id=%d status=%s paragraphs=%d emb=%d",
                 pdf_id, result["status"], result["paragraphs"],
                 result["embeddings_inserted"])
    except Exception as e:
        log.warning("text-index failed pdf_id=%d: %s", pdf_id, e)


def _process_one(session, conn, item: scraper.ScrapedPdf, *, root: Path,
                 known_urls: set[str], delay_s: float, dry_run: bool) -> str:
    """Returns one of: 'skipped', 'downloaded', 'metadata_only', 'error'."""
    if item.source_url in known_urls:
        return "skipped"
    target = scraper.storage_path(root, item)
    if dry_run:
        _save(conn, item, None, None, None)
        conn.commit()
        known_urls.add(item.source_url)
        return "metadata_only"
    try:
        if target.exists() and target.stat().st_size > 0:
            # Already on disk but not in DB — record metadata, skip re-download.
            sha = bytes_size = None  # leave existing nulls; upsert will COALESCE
            pdf_id = _save(conn, item, target, sha, bytes_size)
        else:
            sha, n = scraper.download_pdf(session, item.source_url, target, delay_s=delay_s)
            pdf_id = _save(conn, item, target, sha, n)
        _extract_after_download(conn, pdf_id, item.pq_refs)
        conn.commit()
        known_urls.add(item.source_url)
        return "downloaded"
    except Exception as e:
        log.warning("failed %s: %s", item.source_url, e)
        return "error"


# ---------------- backfill ----------------

def _wayback_catalog(session, conn, *, max_items, delay_s) -> dict:
    """Phase 1: query CDX, upsert metadata-only rows for every archived PDF.

    Doesn't download anything — just populates `hse_pdfs` so we can later
    query the DB to decide which PDFs to fetch.
    """
    known = db.hse_pdf_source_urls(conn)
    log.info("catalog phase: %d source_urls already in DB will be skipped", len(known))
    n_new = n_skip = 0
    for item in scraper.iter_wayback_pdfs(session, delay_s=delay_s, max_items=max_items):
        if item.source_url in known:
            n_skip += 1
            continue
        db.upsert_hse_pdf(
            conn,
            source=item.source,
            source_url=item.source_url,
            index_url=item.index_url,
            filename=item.filename,
            pq_refs=item.pq_refs,
            publication_date=item.publication_date.isoformat() if item.publication_date else None,
            local_path=None,
            sha256=None,
            bytes_size=None,
            fetched_at=None,
        )
        known.add(item.source_url)
        n_new += 1
        if (n_new % 500) == 0:
            conn.commit()
            log.info("catalog progress: cataloged=%d", n_new)
    conn.commit()
    log.info("catalog done: cataloged=%d already-known=%d", n_new, n_skip)
    return {"cataloged": n_new, "skipped": n_skip}


def _wayback_pending_rows(conn, *, matched_only: bool):
    """Cataloged Wayback rows that haven't been downloaded yet."""
    if matched_only:
        q = """SELECT id, source_url, filename, pq_refs_json
                 FROM hse_pdfs
                WHERE source='hse_wayback'
                  AND (local_path IS NULL OR local_path = '')
                  AND id IN (
                    SELECT DISTINCT j.hse_pdf_id
                      FROM hse_pdf_pqs j
                      JOIN questions q ON q.pq_ref = j.pq_ref
                  )
                ORDER BY id"""
    else:
        q = """SELECT id, source_url, filename, pq_refs_json
                 FROM hse_pdfs
                WHERE source='hse_wayback'
                  AND (local_path IS NULL OR local_path = '')
                ORDER BY id"""
    return conn.execute(q).fetchall()


def _wayback_download_pending(session, conn, *, matched_only: bool,
                              delay_s: float, max_items: int | None = None) -> dict:
    """Phase 2/3: walk cataloged-but-not-downloaded rows and fetch the PDFs."""
    rows = _wayback_pending_rows(conn, matched_only=matched_only)
    total = len(rows)
    if total == 0:
        log.info("nothing to download (matched_only=%s)", matched_only)
        return {"downloaded": 0, "errors": 0, "total": 0}
    log.info("download queue: %d pending rows (matched_only=%s)", total, matched_only)
    n_ok = n_err = 0
    for i, r in enumerate(rows):
        if max_items is not None and (n_ok + n_err) >= max_items:
            log.info("hit --max-cdx cap, stopping at %d", max_items)
            break
        try:
            refs = json.loads(r["pq_refs_json"])
        except (json.JSONDecodeError, TypeError):
            refs = []
        item = scraper.ScrapedPdf(
            source="hse_wayback",
            source_url=r["source_url"],
            index_url=None,
            filename=r["filename"],
            pq_refs=refs,
            publication_date=None,
        )
        target = scraper.storage_path(cfg.HSE_PDF_DIR, item)
        try:
            if target.exists() and target.stat().st_size > 0:
                # Already on disk from an earlier run / external copy — record
                # the path but leave sha/bytes for COALESCE-preserve.
                sha = bytes_size = None
            else:
                sha, bytes_size = scraper.download_pdf(
                    session, item.source_url, target, delay_s=delay_s
                )
            pdf_id, _ = db.upsert_hse_pdf(
                conn,
                source=item.source,
                source_url=item.source_url,
                index_url=None,
                filename=item.filename,
                pq_refs=item.pq_refs,
                publication_date=None,
                local_path=str(target.relative_to(cfg.ROOT)).replace("\\", "/"),
                sha256=sha,
                bytes_size=bytes_size,
                fetched_at=datetime.utcnow().isoformat(timespec="seconds") if sha else None,
            )
            _extract_after_download(conn, pdf_id, item.pq_refs)
            conn.commit()
            n_ok += 1
        except Exception as e:
            log.warning("download failed id=%d url=%s: %s", r["id"], r["source_url"], e)
            n_err += 1
        if (i + 1) % 50 == 0:
            log.info("download progress: %d/%d (ok=%d err=%d)", i + 1, total, n_ok, n_err)
    log.info("download done: ok=%d err=%d total_in_queue=%d", n_ok, n_err, total)
    return {"downloaded": n_ok, "errors": n_err, "total": total}


def cmd_backfill(args) -> int:
    log_path = _configure_logging("hse-backfill")
    cfg.ensure_dirs()
    cfg.HSE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    do_live = args.live or args.all
    do_wb = args.wayback or args.all
    if not (do_live or do_wb):
        print("error: pass at least one of --live / --wayback / --all", file=sys.stderr)
        return 2

    log.info("=== HSE backfill start (live=%s wayback=%s catalog_only=%s priority=%s) ===",
             do_live, do_wb, args.catalog_only, args.priority)
    session = scraper.make_session()

    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        known = db.hse_pdf_source_urls(conn)
        log.info("already have %d HSE PDFs recorded", len(known))

        if do_live:
            stats = {"skipped": 0, "downloaded": 0, "metadata_only": 0, "error": 0}
            n_visited = 0
            for pub_url in scraper.iter_live_listing_pages(
                session,
                start_page=args.start_page,
                stop_page=args.max_pages,
                delay_s=args.delay_live,
            ):
                n_visited += 1
                item = scraper.fetch_live_publication(session, pub_url, delay_s=args.delay_live)
                if item is None:
                    stats["error"] += 1
                    continue
                if item.source_url in known:
                    stats["skipped"] += 1
                    continue
                outcome = _process_one(session, conn, item, root=cfg.HSE_PDF_DIR,
                                       known_urls=known, delay_s=args.delay_live,
                                       dry_run=args.dry_run)
                stats[outcome] += 1
                if (n_visited % 25) == 0:
                    log.info("live progress: visited=%d stats=%s", n_visited, stats)
            log.info("live done: visited=%d stats=%s", n_visited, stats)

        if do_wb:
            # Phase 1: catalog. Skip if we already have rows unless --recatalog.
            wb_count = conn.execute(
                "SELECT COUNT(*) FROM hse_pdfs WHERE source='hse_wayback'"
            ).fetchone()[0]
            if wb_count == 0 or args.recatalog:
                _wayback_catalog(session, conn,
                                 max_items=args.max_cdx, delay_s=args.delay_wb)
            else:
                log.info("catalog phase: %d wayback rows already in DB, skipping CDX query "
                         "(pass --recatalog to re-query)", wb_count)

            # Phase 2/3: download (unless catalog-only).
            if args.catalog_only:
                log.info("--catalog-only: skipping download phase")
            else:
                _wayback_download_pending(
                    session, conn,
                    matched_only=(args.priority == "matched"),
                    delay_s=args.delay_wb,
                    max_items=args.max_downloads,
                )

        s = db.hse_pdf_stats(conn)
    log.info("=== HSE backfill complete: %s ===", s)
    log.info("log: %s", log_path)
    print(f"done. log: {log_path}")
    return 0


# ---------------- daily incremental ----------------

def cmd_ingest(args) -> int:
    log_path = _configure_logging("hse-ingest")
    cfg.ensure_dirs()
    cfg.HSE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    log.info("=== HSE incremental ingest start ===")
    session = scraper.make_session()

    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        known = db.hse_pdf_source_urls(conn)
        log.info("known PDFs in DB: %d", len(known))

        stats = {"skipped": 0, "downloaded": 0, "error": 0}
        consecutive_skips = 0
        n_visited = 0
        for pub_url in scraper.iter_live_listing_pages(
            session, start_page=1, stop_page=args.max_pages, delay_s=args.delay_live,
        ):
            n_visited += 1
            item = scraper.fetch_live_publication(session, pub_url, delay_s=args.delay_live)
            if item is None:
                stats["error"] += 1
                continue
            if item.source_url in known:
                stats["skipped"] += 1
                consecutive_skips += 1
                # Once we've seen N publications in a row we already have, the
                # rest of the listing is older still — stop.
                if consecutive_skips >= args.stop_after_skips:
                    log.info("hit %d consecutive known PDFs, stopping incremental walk",
                             consecutive_skips)
                    break
                continue
            consecutive_skips = 0
            outcome = _process_one(session, conn, item, root=cfg.HSE_PDF_DIR,
                                   known_urls=known, delay_s=args.delay_live,
                                   dry_run=False)
            stats[outcome if outcome in stats else "error"] += 1
            if (n_visited % 10) == 0:
                log.info("ingest progress: visited=%d stats=%s", n_visited, stats)
        log.info("incremental done: visited=%d stats=%s", n_visited, stats)
        s = db.hse_pdf_stats(conn)
    log.info("=== done: %s ===", s)
    log.info("log: %s", log_path)
    return 0


# ---------------- targeted backfill of missing HSE answers ----------------

def _missing_answer_candidates(conn, *, all_unlinked: bool, days: int | None) -> list[str]:
    """Answered PQs in our corpus that have no HSE PDF linked yet.

    Default keeps only PQs whose answer text defers to the HSE for a direct
    reply (``answer_defers_to_hse``) — the ones that actually receive a
    supplementary PDF. ``all_unlinked=True`` drops that filter. ``days``
    restricts to PQs answered within the last N days (rolling window).
    """
    sql = [
        "SELECT q.pq_ref, q.date_answered, a.answer_text",
        "  FROM questions q LEFT JOIN answers a ON a.id = q.answer_id",
        " WHERE q.answer_status = 'answered'",
        "   AND NOT EXISTS (SELECT 1 FROM hse_pdf_pqs l WHERE l.pq_ref = q.pq_ref)",
    ]
    params: list = []
    if days is not None:
        cutoff = (datetime.utcnow().date() - timedelta(days=days)).isoformat()
        sql.append("   AND q.date_answered >= ?")
        params.append(cutoff)
    sql.append(" ORDER BY q.date_answered DESC, q.pq_ref")
    rows = conn.execute("\n".join(sql), params).fetchall()
    if all_unlinked:
        return [r["pq_ref"] for r in rows]
    return [r["pq_ref"] for r in rows if answer_defers_to_hse(r["answer_text"])]


def cmd_backfill_missing(args) -> int:
    log_path = _configure_logging("hse-backfill-missing")
    cfg.ensure_dirs()
    cfg.HSE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    session = scraper.make_session()
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        known = db.hse_pdf_source_urls(conn)
        refs = _missing_answer_candidates(conn, all_unlinked=args.all, days=args.days)
        if args.limit:
            refs = refs[: args.limit]
        log.info("=== HSE backfill-missing start: %d candidate ref(s) "
                 "(all_unlinked=%s, days=%s, limit=%s) ===",
                 len(refs), args.all, args.days, args.limit)

        stats = {"checked": 0, "pubs_found": 0, "downloaded": 0,
                 "linked": 0, "none": 0, "error": 0}
        for i, ref in enumerate(refs, 1):
            stats["checked"] += 1
            try:
                pub_urls = scraper.search_live_by_ref(session, ref, delay_s=args.delay_live)
            except Exception as e:  # noqa: BLE001
                log.warning("search failed for %s: %s", ref, e)
                stats["error"] += 1
                continue
            if not pub_urls:
                stats["none"] += 1
            else:
                stats["pubs_found"] += 1
            for pub_url in pub_urls:
                item = scraper.fetch_live_publication(session, pub_url, delay_s=args.delay_live)
                if item is None:
                    stats["error"] += 1
                    continue
                # HSE's search matched this ref even if the PDF filename names a
                # different lead ref (bundled answers). upsert_hse_pdf replaces
                # the whole junction, so pass parsed refs ∪ {ref} — never just
                # {ref} — to add the missing link without clobbering existing.
                if ref not in item.pq_refs:
                    item.pq_refs = item.pq_refs + [ref]
                try:
                    target = scraper.storage_path(cfg.HSE_PDF_DIR, item)
                    have_file = target.exists() and target.stat().st_size > 0
                    if item.source_url in known or have_file:
                        # Already held on disk / in DB — (re)link only.
                        sha = bytes_size = None
                        local = target if have_file else None
                    else:
                        sha, bytes_size = scraper.download_pdf(
                            session, item.source_url, target, delay_s=args.delay_live)
                        local = target
                        stats["downloaded"] += 1
                    pdf_id = _save(conn, item, local, sha, bytes_size)
                    _extract_after_download(conn, pdf_id, item.pq_refs)
                    conn.commit()
                    known.add(item.source_url)
                    stats["linked"] += 1
                    log.info("linked %s -> pdf_id=%d (%s)", ref, pdf_id, item.filename[:55])
                except Exception as e:  # noqa: BLE001
                    log.warning("backfill failed for %s (%s): %s", ref, pub_url, e)
                    stats["error"] += 1
            if (i % 25) == 0:
                log.info("progress: %d/%d stats=%s", i, len(refs), stats)
        log.info("=== backfill-missing done: stats=%s ===", stats)
        log.info("log: %s", log_path)
    return 0


# ---------------- stats ----------------

def cmd_stats(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        s = db.hse_pdf_stats(conn)
    print(f"HSE PDFs total: {s['total']}")
    for src, n in (s["by_source"] or {}).items():
        print(f"  {src}: {n}")
    print(f"PQ refs in your questions table with at least one matching HSE PDF: {s['matched_pq_refs']}")
    print(f"Paragraph rows (extracted): {s['paragraphs']}")
    for st, n in (s["by_extraction"] or {}).items():
        print(f"  extraction[{st}]: {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pq-tracker-hse",
                                description="Scrape & track HSE PQ response PDFs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("backfill", help="One-off full scrape (live + Wayback). Resumable.")
    pb.add_argument("--live", action="store_true", help="Walk the live HSE publications listing.")
    pb.add_argument("--wayback", action="store_true", help="Pull every archived PDF via CDX.")
    pb.add_argument("--all", action="store_true", help="Equivalent to --live --wayback.")
    pb.add_argument("--max-pages", type=int, default=None,
                    help="Stop after this many live listing pages (default: walk all).")
    pb.add_argument("--start-page", type=int, default=1,
                    help="First live listing page to fetch.")
    pb.add_argument("--max-cdx", type=int, default=None,
                    help="Cap the number of CDX rows ingested in the catalog phase.")
    pb.add_argument("--max-downloads", type=int, default=None,
                    help="Cap the number of PDFs downloaded in this run (matched or all).")
    pb.add_argument("--delay-live", type=float, default=1.0,
                    help="Seconds between live-site requests.")
    pb.add_argument("--delay-wb", type=float, default=2.0,
                    help="Seconds between Wayback requests (CDX + PDF downloads).")
    pb.add_argument("--catalog-only", action="store_true",
                    help="Wayback: query CDX and store metadata only; don't download PDFs. "
                         "Quick (~5 min) — populates the index so later runs can prioritize.")
    pb.add_argument("--recatalog", action="store_true",
                    help="Wayback: re-query CDX even if rows already exist (default is to "
                         "skip CDX if we've cataloged before).")
    pb.add_argument("--priority", choices=["matched", "all"], default="all",
                    help="Wayback download order: 'matched' = only PDFs whose pq_ref joins "
                         "a row in your questions table; 'all' = every pending PDF "
                         "(default). 'matched' auto-catalogs first if needed.")
    pb.add_argument("--dry-run", action="store_true",
                    help="(Live only) Record metadata without downloading PDFs.")
    pb.set_defaults(func=cmd_backfill)

    pi = sub.add_parser("ingest", help="Incremental walk of the live site; stops when "
                                       "we hit consecutive known PDFs.")
    pi.add_argument("--delay-live", type=float, default=1.0)
    pi.add_argument("--stop-after-skips", type=int, default=20,
                    help="Stop after this many already-known PDFs in a row.")
    pi.add_argument("--max-pages", type=int, default=20,
                    help="Hard upper bound on listing pages walked.")
    pi.set_defaults(func=cmd_ingest)

    pm = sub.add_parser("backfill-missing",
                        help="Targeted ?query= lookup for answered PQs with no HSE link. "
                             "Reaches answers back-published deep in the listing that the "
                             "page-walk can't.")
    pm.add_argument("--all", action="store_true",
                    help="Check every unlinked answered PQ. Default: only those whose answer "
                         "defers to the HSE for a direct reply (~96%% of real HSE answers, "
                         "far fewer queries).")
    pm.add_argument("--days", type=int, default=None,
                    help="Only PQs answered within the last N days (rolling window for "
                         "scheduled runs). Default: no limit.")
    pm.add_argument("--limit", type=int, default=None,
                    help="Cap the number of refs checked this run.")
    pm.add_argument("--delay-live", type=float, default=1.0,
                    help="Seconds between live-site requests.")
    pm.set_defaults(func=cmd_backfill_missing)

    ps = sub.add_parser("stats", help="Print counts of HSE PDFs in the DB.")
    ps.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
