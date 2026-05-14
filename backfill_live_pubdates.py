r"""One-off: backfill `publication_date` on existing hse_live rows.

The live scraper hard-coded publication_date=None until 2026-05-14; we now parse
"Published: <Month YYYY>" out of the publication index page. This script revisits
each existing hse_live row's `index_url`, parses the date, and writes it.

Run:
    .\.venv\Scripts\python.exe backfill_live_pubdates.py                    # all rows w/ NULL pub date
    .\.venv\Scripts\python.exe backfill_live_pubdates.py --limit 50         # smoke test
    .\.venv\Scripts\python.exe backfill_live_pubdates.py --delay-s 0.3      # tighten delay (default 0.5s)
    .\.venv\Scripts\python.exe backfill_live_pubdates.py --force            # also revisit rows that already have a date

Resumable: by default skips rows where publication_date IS NOT NULL.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from pq_tracker import config as cfg  # noqa: E402
from pq_tracker.hse_scraper import (  # noqa: E402
    _LIVE_PUBDATE_RE, _LIVE_UPDATE_RE, _parse_month_year, USER_AGENT,
)

log = logging.getLogger("backfill_live_pubdates")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true",
                        help="also revisit rows where publication_date IS NOT NULL")
    parser.add_argument("--delay-s", type=float, default=0.5,
                        help="seconds between requests (default 0.5)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    conn = sqlite3.connect(cfg.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row

    where = "source = 'hse_live' AND index_url IS NOT NULL AND index_url != ''"
    if not args.force:
        where += " AND publication_date IS NULL"
    todo = conn.execute(
        f"SELECT id, index_url FROM hse_pdfs WHERE {where} ORDER BY id"
    ).fetchall()
    log.info("rows to process: %d (force=%s)", len(todo), args.force)

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    updated = no_date = errors = 0
    started = time.time()
    for i, row in enumerate(todo, start=1):
        if args.limit and updated >= args.limit:
            log.info("limit %d reached", args.limit)
            break
        try:
            time.sleep(args.delay_s)
            r = s.get(row["index_url"], timeout=20)
            if r.status_code != 200:
                log.warning("%s: HTTP %s", row["index_url"], r.status_code)
                errors += 1
                continue
            pub_m = _LIVE_PUBDATE_RE.search(r.text)
            upd_m = _LIVE_UPDATE_RE.search(r.text)
            d = _parse_month_year(pub_m.group(1)) if pub_m else None
            if d is None and upd_m:
                d = _parse_month_year(upd_m.group(1))
            if d is None:
                no_date += 1
                log.warning("no date found on %s", row["index_url"])
                continue
            conn.execute(
                "UPDATE hse_pdfs SET publication_date = ? WHERE id = ?",
                (d.isoformat(), row["id"]),
            )
            updated += 1
            if updated % 50 == 0:
                elapsed = time.time() - started
                conn.commit()
                log.info("checkpoint: updated=%d  no_date=%d  errors=%d  rate=%.1f/s",
                         updated, no_date, errors, updated / max(elapsed, 0.001))
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("%s: %s", row["index_url"], e)

    conn.commit()
    conn.close()
    elapsed = time.time() - started
    log.info("done. updated=%d  no_date=%d  errors=%d  elapsed=%.1fs",
             updated, no_date, errors, elapsed)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
