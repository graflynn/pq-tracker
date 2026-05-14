r"""Delete on-disk HSE PDFs whose pq_refs no longer link to any row in `questions`.

After pruning the question corpus, some HSE PDFs we'd downloaded for those PQs
are now "orphans" — file on disk, no connection to a tracked PQ. This script
deletes the file and nulls `local_path` on the row (keeping the catalog entry,
so if a future ingest brings the PQ back in scope we know the PDF exists and
can re-download).

Defaults to dry-run. By default targets wayback only — live PDFs may still
match a future re-ingest with different search_terms, so we keep those.

Run:
    .\.venv\Scripts\python.exe vacuum_hse_orphans.py                       # dry-run, wayback only
    .\.venv\Scripts\python.exe vacuum_hse_orphans.py --apply               # commit deletes
    .\.venv\Scripts\python.exe vacuum_hse_orphans.py --include-live        # also vacuum unmatched live PDFs
    .\.venv\Scripts\python.exe vacuum_hse_orphans.py --apply --include-live --yes
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pq_tracker import config as cfg  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="commit (default is dry-run)")
    parser.add_argument("--include-live", action="store_true",
                        help="also vacuum unmatched live PDFs (default: wayback only)")
    parser.add_argument("--yes", action="store_true",
                        help="skip confirmation prompt with --apply")
    parser.add_argument("--sample", type=int, default=10)
    args = parser.parse_args()

    sources = ["hse_wayback"]
    if args.include_live:
        sources.append("hse_live")
    placeholders = ",".join("?" * len(sources))

    conn = sqlite3.connect(cfg.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT p.id, p.source, p.local_path
              FROM hse_pdfs p
             WHERE p.source IN ({placeholders})
               AND p.local_path IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM hse_pdf_pqs j
                     JOIN questions q ON q.pq_ref = j.pq_ref
                    WHERE j.hse_pdf_id = p.id
               )
            ORDER BY p.id""",
        sources,
    ).fetchall()
    print(f"sources scanned: {sources}")
    print(f"orphan PDFs (downloaded but no tracked PQ): {len(rows)}")

    # Compute disk usage and find missing-on-disk discrepancies.
    bytes_total = 0
    missing_on_disk = 0
    files_to_delete: list[tuple[int, Path]] = []
    for r in rows:
        f = cfg.ROOT / r["local_path"]
        if f.exists():
            try:
                bytes_total += f.stat().st_size
            except OSError:
                pass
            files_to_delete.append((r["id"], f))
        else:
            missing_on_disk += 1
            # Still nullify local_path since the file isn't there.
            files_to_delete.append((r["id"], f))
    print(f"on-disk size to free: {bytes_total / (1024*1024):.1f} MB")
    if missing_on_disk:
        print(f"({missing_on_disk} rows had local_path set but no file on disk — will null anyway)")

    if rows and args.sample > 0:
        print(f"\nsample (first {min(args.sample, len(rows))}):")
        for r in rows[:args.sample]:
            print(f"  id={r['id']:>5}  source={r['source']:13s}  {r['local_path']}")

    if not args.apply:
        print("\ndry-run only — re-run with --apply to commit.")
        return 0
    if not rows:
        print("\nnothing to vacuum.")
        return 0
    if not args.yes:
        ans = input(f"\nDelete {len(files_to_delete)} files and null their local_path? type 'yes' to confirm: ").strip().lower()
        if ans != "yes":
            print("aborted.")
            return 1

    deleted = 0
    fs_errors = 0
    for pid, path in files_to_delete:
        try:
            if path.exists():
                path.unlink()
                deleted += 1
        except OSError as e:
            fs_errors += 1
            print(f"  failed to delete {path}: {e}")
            continue
        conn.execute("UPDATE hse_pdfs SET local_path = NULL WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    print(f"\ndeleted {deleted} files, freed ~{bytes_total / (1024*1024):.1f} MB. "
          f"{fs_errors} filesystem errors.")
    return 0 if fs_errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
