r"""Prune PQ rows that no longer match the current `search_terms` in topics.yaml.

The split between `search_terms` (ingest gate) and `keywords` (auto-tags) was
introduced 2026-05-14. Existing rows were gated on the old conflated list, so
the historical corpus contains PQs that don't match the new search_terms. This
script removes those rows so the DB reflects only what the current gate would
have admitted.

Default mode is dry-run. Tweak topics.yaml, re-run, inspect the count, repeat.
When happy, pass --apply.

Run:
    .\.venv\Scripts\python.exe prune_corpus.py                   # dry-run, full summary
    .\.venv\Scripts\python.exe prune_corpus.py --sample 25        # show 25 sample rows
    .\.venv\Scripts\python.exe prune_corpus.py --apply            # commit deletes
    .\.venv\Scripts\python.exe prune_corpus.py --apply --yes      # skip confirm

Safety:
- Refuses to delete any row with manual data (notes / constituent / hse_pdf_url).
  If any such row would be pruned, prints a list and aborts even with --apply.
- One transaction. Cascades to `tags` and `embeddings` via FK. Manually cleans
  `hse_pdf_pqs` (no cascade on pq_ref). Rebuilds FTS5 index.
- No network. No XML re-fetch. Pure local rescan of stored text.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pq_tracker import config as cfg  # noqa: E402
from pq_tracker import db  # noqa: E402
from pq_tracker.matching import match_keywords  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="commit the deletes (default is dry-run)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt when used with --apply")
    parser.add_argument("--sample", type=int, default=10,
                        help="how many sample pq_refs to print in dry-run output (default 10)")
    args = parser.parse_args()

    settings = cfg.load_config()
    print(f"search_terms: {settings.search_terms}")
    print()

    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        rows = conn.execute(
            """SELECT q.pq_ref, q.date_asked, q.raw_question_showas,
                      q.question_text, a.answer_text,
                      COALESCE(q.notes,'') AS notes,
                      COALESCE(q.constituent,'') AS constituent,
                      COALESCE(q.hse_pdf_url,'') AS hse_pdf_url
                 FROM questions q
                 LEFT JOIN answers a ON a.id = q.answer_id"""
        ).fetchall()
        total = len(rows)

        kill: list[tuple[str, str]] = []
        kill_with_manual: list[str] = []
        for r in rows:
            text = "\n".join(filter(None, (
                r["raw_question_showas"], r["question_text"], r["answer_text"]
            )))
            if match_keywords(text, settings.search_terms):
                continue
            kill.append((r["pq_ref"], r["date_asked"]))
            if r["notes"] or r["constituent"] or r["hse_pdf_url"]:
                kill_with_manual.append(r["pq_ref"])

        kept = total - len(kill)
        print(f"total rows:        {total}")
        print(f"would keep:        {kept}")
        print(f"would delete:      {len(kill)}")
        print()

        if kill_with_manual:
            print(f"REFUSING: {len(kill_with_manual)} of the rows targeted for deletion have")
            print("manual data (notes / constituent / hse_pdf_url). Listed below — either")
            print("widen search_terms in topics.yaml or clear those fields, then re-run.")
            for ref in kill_with_manual[:50]:
                print(f"  {ref}")
            if len(kill_with_manual) > 50:
                print(f"  ... and {len(kill_with_manual) - 50} more")
            return 2

        if args.sample > 0 and kill:
            print(f"sample of {min(args.sample, len(kill))} to-be-deleted rows:")
            for pq_ref, date_asked in kill[:args.sample]:
                print(f"  {pq_ref}  ({date_asked})")
            print()

        if not args.apply:
            print("dry-run only — re-run with --apply to commit.")
            return 0
        if not kill:
            print("nothing to prune.")
            return 0
        if not args.yes:
            ans = input(f"Delete {len(kill)} rows? type 'yes' to confirm: ").strip().lower()
            if ans != "yes":
                print("aborted.")
                return 1

        # Single transaction so any failure rolls back cleanly. Foreign-key
        # cascades handle `tags` and `embeddings`; hse_pdf_pqs needs manual
        # cleanup because its FK is on hse_pdf_id, not pq_ref.
        refs = [r[0] for r in kill]
        BATCH = 500  # SQLite param limit is 999; stay well under it
        for i in range(0, len(refs), BATCH):
            chunk = refs[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM hse_pdf_pqs WHERE pq_ref IN ({placeholders})", chunk
            )
            conn.execute(
                f"DELETE FROM questions   WHERE pq_ref IN ({placeholders})", chunk
            )
        conn.commit()

        # FTS5 is now inline-content and we maintain it manually via
        # refresh_fts_for_pq on writes. After bulk DELETEs we need to purge
        # the FTS rows for the pruned pq_refs to keep search consistent.
        for i in range(0, len(refs), BATCH):
            chunk = refs[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM questions_fts WHERE pq_ref IN ({placeholders})", chunk
            )
        conn.commit()

        remaining = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        print(f"deleted {len(kill)} rows. questions now contains {remaining} rows.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
