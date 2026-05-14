r"""One-off fix script: for every answered PQ in the corpus, download the
Akoma Ntoso XML, store the raw bytes in the new `xml_raw` column, and re-run
the same extraction the live ingest uses to refresh `question_text`,
`answer_text`, and `minister_name`. Mirrors the per-row logic in ingest.run()
exactly — including the `qa.question_text or entry.show_as` fallback that
keeps `question_text` non-null when the XML's eId disagrees with the index
listing.

Permalinks: the `init_schema` migration strips dead `/pq_NNN/` forms in one
shot on first app start, so this script doesn't repeat that.

Run:
    .\.venv\Scripts\python.exe reformat_corpus.py
    .\.venv\Scripts\python.exe reformat_corpus.py --limit 50         # smoke test
    .\.venv\Scripts\python.exe reformat_corpus.py --force            # ignore xml_raw IS NOT NULL

Resumable: by default, skips rows that already have xml_raw populated.
Grouped questions share a single XML file — we cache by xml_url in-process
so the same file isn't downloaded N times.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from lxml import etree

# Reuse the existing client + extraction so behaviour stays consistent with
# the daily ingest path.
sys.path.insert(0, str(Path(__file__).parent))
from pq_tracker import config as cfg  # noqa: E402
from pq_tracker import db  # noqa: E402
from pq_tracker.oireachtas import OireachtasClient, _extract_qa  # noqa: E402

log = logging.getLogger("reformat_corpus")


def _eid_from_uri(uri: str) -> str:
    """Last path segment of question_uri (e.g. .../pq_405 → 'pq_405')."""
    return uri.rsplit("/", 1)[-1] if uri else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N updates (0 = all)")
    parser.add_argument("--force", action="store_true",
                        help="re-process rows even if xml_raw is already set")
    parser.add_argument("--rate-ms", type=int, default=250,
                        help="minimum gap between XML fetches, in ms")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    client = OireachtasClient(fetch_delay_ms=args.rate_ms)
    xml_cache: dict[str, bytes] = {}  # xml_url -> raw bytes (one PQ file may
                                       # cover many grouped questions)

    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)  # ensures xml_raw column + permalink migration

    where = "answer_status = 'answered' AND xml_url IS NOT NULL AND xml_url != ''"
    if not args.force:
        where += " AND xml_raw IS NULL"
    todo = conn.execute(
        f"SELECT pq_ref, question_uri, xml_url, raw_question_showas "
        f"FROM questions WHERE {where} ORDER BY pq_ref"
    ).fetchall()
    log.info("rows to process: %d (force=%s)", len(todo), args.force)

    processed = updated = errors = skipped = 0
    started = time.time()

    for row in todo:
        if args.limit and updated >= args.limit:
            log.info("limit %d reached", args.limit)
            break
        pq_ref = row["pq_ref"]
        xml_url = row["xml_url"]
        e_id = _eid_from_uri(row["question_uri"] or "")
        if not e_id:
            log.warning("%s: empty e_id; skipping", pq_ref)
            skipped += 1
            continue
        try:
            xml_bytes = xml_cache.get(xml_url)
            if xml_bytes is None:
                # fetch_xml does its own parsing + extraction; for cache-control
                # we do the raw fetch ourselves.
                time.sleep(args.rate_ms / 1000.0)
                r = client.s.get(xml_url, timeout=60,
                                 headers={"Accept": "application/xml"})
                if r.status_code != 200:
                    log.warning("%s: %s -> HTTP %s", pq_ref, xml_url, r.status_code)
                    errors += 1
                    continue
                xml_bytes = r.content
                xml_cache[xml_url] = xml_bytes
            root = etree.fromstring(xml_bytes)
            qa = _extract_qa(root, e_id)
        except (etree.XMLSyntaxError, Exception) as e:  # noqa: BLE001
            log.warning("%s: %s", pq_ref, e)
            errors += 1
            continue
        # Same fallback ingest.run() uses (ingest.py:140): when the XML's
        # <question eId=...> body comes back empty (rare API/XML eId
        # mismatch, ~0.03% of rows), fall back to the show_as text we
        # already have. raw_question_showas is NOT NULL in the schema, so
        # question_text is guaranteed non-empty.
        question_text = qa.question_text or row["raw_question_showas"]
        if not qa.question_text:
            log.warning("%s: empty XML extraction (eId=%s not in %s) "
                        "— using raw_question_showas fallback",
                        pq_ref, e_id, xml_url)

        now = datetime.utcnow().isoformat(timespec="seconds")
        conn.execute(
            """UPDATE questions
                  SET xml_raw = ?,
                      question_text = ?,
                      answer_text = ?,
                      minister_name = COALESCE(?, minister_name),
                      last_updated_at = ?
                WHERE pq_ref = ?""",
            (xml_bytes,
             question_text,
             qa.answer_text,
             qa.minister_name,
             now, pq_ref),
        )
        updated += 1
        processed += 1
        if updated % 50 == 0:
            elapsed = time.time() - started
            rate = updated / max(elapsed, 0.001)
            log.info("checkpoint: updated=%d  errors=%d  skipped=%d  "
                     "rate=%.1f/s  cache_size=%d",
                     updated, errors, skipped, rate, len(xml_cache))
            conn.commit()

    conn.commit()
    elapsed = time.time() - started
    log.info("done. processed=%d updated=%d errors=%d skipped=%d  "
             "cache_hits_saved=%d  elapsed=%.1fs",
             processed, updated, errors, skipped,
             # rough estimate: # of rows minus distinct URLs we actually fetched
             max(0, processed - len(xml_cache)), elapsed)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
