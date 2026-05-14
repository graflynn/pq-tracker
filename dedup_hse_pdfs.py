r"""Collapse duplicate HSE PDFs (same sha256, different source_url).

Why this exists. The same PDF gets published under multiple URLs in two
patterns:
  - hse_live:    one PDF answers many grouped PQs and the publication API
                 emits a per-PQ download URL (PQ_3241-26..., PQ_3067-26..., ...
                 all serve the same bytes).
  - hse_wayback: CDX has multiple snapshots of the same .../personalpq/ URL.

Each duplicate burns disk, embeddings, and FTS rows. This pass keeps one
canonical row per sha256 and re-points the junction so PQ links survive.

Survivor selection per sha256 group (downloaded rows only):
  1. text_extraction_status: done > NULL > empty > failed
  2. source: hse_live > hse_wayback (stable canonical URL)
  3. id: smallest (stable across runs)

Loser handling per group, in one transaction each:
  1. INSERT OR IGNORE junction rows from each loser to survivor
  2. Update survivor.pq_refs_json to the union across the group
  3. For each loser: delete its hse_paragraphs_fts rows by rowid (FTS5 mirror
     is manually maintained — schema cascade does not reach it), then
     DELETE FROM hse_pdfs WHERE id=loser (cascades junction, paragraphs,
     embeddings via FK ON DELETE CASCADE), then unlink the on-disk file.

Defaults to dry-run.

    .\.venv\Scripts\python.exe dedup_hse_pdfs.py                  # dry-run
    .\.venv\Scripts\python.exe dedup_hse_pdfs.py --apply
    .\.venv\Scripts\python.exe dedup_hse_pdfs.py --apply --yes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pq_tracker import config as cfg  # noqa: E402


# Lower number = preferred survivor.
_STATUS_RANK = {"done": 0, None: 1, "empty": 2, "failed": 3}
_SOURCE_RANK = {"hse_live": 0, "hse_wayback": 1}


def _survivor_key(row: sqlite3.Row) -> tuple:
    return (
        _STATUS_RANK.get(row["text_extraction_status"], 9),
        _SOURCE_RANK.get(row["source"], 9),
        row["id"],
    )


def _union_pq_refs(rows: list[sqlite3.Row]) -> list[str]:
    """Merge pq_refs_json across the group, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        try:
            refs = json.loads(r["pq_refs_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            refs = []
        for x in refs:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
    return out


def _process_group(conn: sqlite3.Connection, sha: str, *, apply: bool) -> dict:
    """Plan or execute dedup for one sha256 group. Returns per-group stats."""
    members = conn.execute(
        """SELECT id, source, source_url, local_path, pq_refs_json,
                  bytes, text_extraction_status
             FROM hse_pdfs
            WHERE sha256 = ? AND local_path IS NOT NULL
            ORDER BY id""",
        (sha,),
    ).fetchall()
    if len(members) < 2:
        return {"groups": 0}

    ranked = sorted(members, key=_survivor_key)
    survivor = ranked[0]
    losers = ranked[1:]
    loser_ids = [r["id"] for r in losers]

    union_refs = _union_pq_refs(members)
    bytes_freed = sum((r["bytes"] or 0) for r in losers)

    # Count downstream rows that will go away.
    placeholders = ",".join("?" * len(loser_ids))
    n_paragraphs = conn.execute(
        f"SELECT COUNT(*) FROM hse_paragraphs WHERE hse_pdf_id IN ({placeholders})",
        loser_ids,
    ).fetchone()[0]
    n_embeddings = conn.execute(
        f"SELECT COUNT(*) FROM embeddings WHERE source_pdf_id IN ({placeholders})",
        loser_ids,
    ).fetchone()[0]
    n_junction = conn.execute(
        f"SELECT COUNT(*) FROM hse_pdf_pqs WHERE hse_pdf_id IN ({placeholders})",
        loser_ids,
    ).fetchone()[0]

    if not apply:
        return {
            "groups": 1, "members": len(members), "losers": len(losers),
            "bytes_freed": bytes_freed,
            "paragraphs_freed": n_paragraphs,
            "embeddings_freed": n_embeddings,
            "junctions_collapsed": n_junction,
            "survivor": survivor, "losers_rows": losers, "union_refs": union_refs,
        }

    # APPLY path. One transaction per group keeps blast radius small.
    # 1. Mirror junction rows onto survivor (UNIQUE PK absorbs dupes).
    j_rows = conn.execute(
        f"SELECT pq_ref FROM hse_pdf_pqs WHERE hse_pdf_id IN ({placeholders})",
        loser_ids,
    ).fetchall()
    if j_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO hse_pdf_pqs(hse_pdf_id, pq_ref) VALUES (?, ?)",
            [(survivor["id"], r["pq_ref"]) for r in j_rows],
        )

    # 2. Update survivor pq_refs_json to the union.
    conn.execute(
        "UPDATE hse_pdfs SET pq_refs_json = ? WHERE id = ?",
        (json.dumps(union_refs, ensure_ascii=False), survivor["id"]),
    )

    # 3. Loser teardown. FTS5 mirror first (no cascade trigger), then row.
    para_ids = [r[0] for r in conn.execute(
        f"SELECT id FROM hse_paragraphs WHERE hse_pdf_id IN ({placeholders})",
        loser_ids,
    )]
    if para_ids:
        conn.executemany(
            "DELETE FROM hse_paragraphs_fts WHERE rowid = ?",
            [(i,) for i in para_ids],
        )
    conn.execute(
        f"DELETE FROM hse_pdfs WHERE id IN ({placeholders})",
        loser_ids,
    )

    # 4. On-disk teardown.
    fs_errors = 0
    files_deleted = 0
    for r in losers:
        if not r["local_path"]:
            continue
        f = cfg.ROOT / r["local_path"]
        try:
            if f.exists():
                f.unlink()
                files_deleted += 1
        except OSError as e:
            fs_errors += 1
            print(f"  ! failed to delete {f}: {e}")

    return {
        "groups": 1, "members": len(members), "losers": len(losers),
        "bytes_freed": bytes_freed, "paragraphs_freed": n_paragraphs,
        "embeddings_freed": n_embeddings, "junctions_collapsed": n_junction,
        "files_deleted": files_deleted, "fs_errors": fs_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="commit (default is dry-run)")
    parser.add_argument("--yes", action="store_true",
                        help="skip confirmation prompt with --apply")
    parser.add_argument("--sample", type=int, default=5,
                        help="show this many groups in dry-run preview (default: 5)")
    args = parser.parse_args()

    conn = sqlite3.connect(cfg.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    dup_shas = [r[0] for r in conn.execute(
        """SELECT sha256
             FROM hse_pdfs
            WHERE local_path IS NOT NULL AND sha256 IS NOT NULL
            GROUP BY sha256 HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC, sha256""",
    )]
    print(f"duplicate sha256 groups (downloaded): {len(dup_shas)}")
    if not dup_shas:
        return 0

    # Dry-run pass first — used both as preview and to gather totals.
    total = {"groups": 0, "losers": 0, "bytes_freed": 0,
             "paragraphs_freed": 0, "embeddings_freed": 0,
             "junctions_collapsed": 0}
    samples: list[dict] = []
    for sha in dup_shas:
        s = _process_group(conn, sha, apply=False)
        if not s.get("groups"):
            continue
        total["groups"] += s["groups"]
        total["losers"] += s["losers"]
        total["bytes_freed"] += s["bytes_freed"]
        total["paragraphs_freed"] += s["paragraphs_freed"]
        total["embeddings_freed"] += s["embeddings_freed"]
        total["junctions_collapsed"] += s["junctions_collapsed"]
        if len(samples) < args.sample:
            samples.append(s)

    print(f"\ntotal members across groups: "
          f"{sum(1 for _ in dup_shas) + total['losers']}")
    print(f"  groups:                {total['groups']}")
    print(f"  losers (will delete):  {total['losers']}")
    print(f"  bytes freed:           {total['bytes_freed'] / (1024*1024):.1f} MB")
    print(f"  paragraphs freed:      {total['paragraphs_freed']}")
    print(f"  embeddings freed:      {total['embeddings_freed']}")
    print(f"  junction rows merged:  {total['junctions_collapsed']}")

    if samples:
        print(f"\nsample (top {len(samples)} groups by size):")
        for s in samples:
            sv = s["survivor"]
            print(f"  group: {s['members']} members, keeping "
                  f"id={sv['id']} src={sv['source']} status={sv['text_extraction_status']}")
            print(f"    survivor url: {sv['source_url'][:100]}")
            print(f"    union pq_refs: {s['union_refs']}")
            print(f"    losers: {[r['id'] for r in s['losers_rows']]}")

    if not args.apply:
        print("\ndry-run only — re-run with --apply to commit.")
        conn.close()
        return 0

    if not args.yes:
        ans = input(f"\nCollapse {total['groups']} groups, deleting {total['losers']} duplicate "
                    f"PDFs ({total['bytes_freed'] / (1024*1024):.1f} MB)? type 'yes' to confirm: ").strip().lower()
        if ans != "yes":
            print("aborted.")
            conn.close()
            return 1

    print("\napplying ...")
    applied = {"groups": 0, "losers": 0, "bytes_freed": 0,
               "paragraphs_freed": 0, "embeddings_freed": 0,
               "junctions_collapsed": 0, "files_deleted": 0, "fs_errors": 0}
    for i, sha in enumerate(dup_shas, start=1):
        s = _process_group(conn, sha, apply=True)
        if not s.get("groups"):
            continue
        for k in ("groups", "losers", "bytes_freed", "paragraphs_freed",
                  "embeddings_freed", "junctions_collapsed", "files_deleted",
                  "fs_errors"):
            applied[k] += s.get(k, 0)
        # commit per-group so a fault later doesn't undo earlier groups
        conn.commit()
        if (i % 25) == 0:
            print(f"  progress: {i}/{len(dup_shas)} groups, "
                  f"{applied['files_deleted']} files removed")
    conn.close()

    print(f"\ndone.")
    print(f"  groups collapsed:      {applied['groups']}")
    print(f"  duplicates deleted:    {applied['losers']}")
    print(f"  files removed:         {applied['files_deleted']}")
    print(f"  bytes freed:           {applied['bytes_freed'] / (1024*1024):.1f} MB")
    print(f"  paragraphs cleared:    {applied['paragraphs_freed']}")
    print(f"  embeddings cleared:    {applied['embeddings_freed']}")
    print(f"  junction rows merged:  {applied['junctions_collapsed']}")
    if applied["fs_errors"]:
        print(f"  filesystem errors:     {applied['fs_errors']}")
    return 0 if applied["fs_errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
