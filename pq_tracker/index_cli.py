"""CLI for building and refreshing the search indexes.

Two indexes live in the DB:
  - questions_fts: SQLite FTS5 over questions.question_text + answer_text.
                   Maintained by an explicit rebuild call after bulk changes.
  - embeddings:    BGE-small vectors per (question, answer) of every PQ,
                   chunked for long answers.

Subcommands:
  rebuild-fts            re-build the FTS5 index from `questions`. Cheap (<5s).
  embed-questions        compute embeddings for every PQ. Skips rows we already
                         have (model + chunk count matches). Incremental.
  reembed-all            drop all embeddings and re-build from scratch.
                         Use if the model or chunker changes.
  stats                  show row counts.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import config as cfg
from . import db
# `embeddings` pulls in numpy + fastembed which are heavy. Import lazily inside
# subcommands that actually need them so plain ops like rebuild-fts can run on
# a fresh checkout before `uv sync` finishes.

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
        handlers=handlers, force=True,
    )
    return log_path


def cmd_rebuild_fts(args) -> int:
    _configure_logging("index-fts")
    log.info("rebuilding FTS5 (porter) ...")
    with db.connect(cfg.DB_PATH) as conn:
        # Drop and recreate so existing installs pick up tokenizer changes
        # (FTS5 won't change options on an existing virtual table).
        conn.execute("DROP TABLE IF EXISTS questions_fts")
        # Also drop the now-defunct character-trigram table if a prior version
        # of this script created it.
        conn.execute("DROP TABLE IF EXISTS questions_trigram")
        db.init_schema(conn)
        conn.execute("INSERT INTO questions_fts(questions_fts) VALUES('rebuild')")
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
    log.info("done. questions_fts rows: %d", n)
    return 0


def _existing_signature(conn, model_name: str) -> dict[tuple[str, str], int]:
    """For each (pq_ref, source_type) currently embedded, count how many chunks
    we have using the current model. Used to skip rows we don't need to rebuild."""
    rows = conn.execute(
        """SELECT source_pq_ref, source_type, COUNT(*) AS n
             FROM embeddings
            WHERE model = ?
            GROUP BY source_pq_ref, source_type""",
        (model_name,),
    ).fetchall()
    return {(r["source_pq_ref"], r["source_type"]): r["n"] for r in rows}


def _embed_one_pq(conn, emb, pq_ref: str, question_text: str, answer_text: str | None,
                  existing: dict) -> tuple[int, int]:
    """Embed question and (if present) answer for a PQ. Skips work that's
    already done (matched chunk count). Returns (chunks_inserted, sources_done).
    """
    now = datetime.utcnow().isoformat(timespec="seconds")
    inserted = 0
    sources = 0
    for source_type, text in (("question", question_text), ("answer", answer_text)):
        if not text or not text.strip():
            continue
        chunks = emb.chunk_text(text)
        if not chunks:
            continue
        # Skip if we already have the right number of chunks for this (pq, source).
        # (If we change chunker params we'd want --rebuild instead.)
        if existing.get((pq_ref, source_type)) == len(chunks):
            continue
        # Re-embed this source for this PQ: clear old rows, write new.
        emb.delete_for_pq(conn, pq_ref, source_type=source_type)
        vecs = emb.embed_texts(chunks)
        rows = []
        for i, (chunk, v) in enumerate(zip(chunks, vecs)):
            rows.append((source_type, pq_ref, None, i, chunk,
                         emb.MODEL_NAME, emb.DIMS, emb.pack(v), now))
        emb.insert_embeddings(conn, rows)
        inserted += len(rows)
        sources += 1
    return inserted, sources


def cmd_embed_questions(args) -> int:
    log_path = _configure_logging("index-embed")
    from . import embeddings as emb
    log.info("embedding questions (model=%s)", emb.MODEL_NAME)
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        existing = _existing_signature(conn, emb.MODEL_NAME)
        total = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        log.info("questions in DB: %d  rows already embedded: %d",
                 total, len({k[0] for k in existing}))
        rows = conn.execute(
            "SELECT pq_ref, question_text, answer_text FROM questions ORDER BY pq_ref"
        ).fetchall()
        n_pq = n_chunks = 0
        for i, r in enumerate(rows):
            try:
                added, sources = _embed_one_pq(conn, emb, r["pq_ref"], r["question_text"],
                                               r["answer_text"], existing)
                if sources:
                    n_pq += 1
                    n_chunks += added
            except Exception as e:
                log.warning("embed failed for %s: %s", r["pq_ref"], e)
            if (i + 1) % 100 == 0:
                conn.commit()
                log.info("progress: scanned=%d/%d embedded_pqs=%d chunks_added=%d",
                         i + 1, total, n_pq, n_chunks)
        conn.commit()
    log.info("done. embedded_pqs=%d chunks_added=%d", n_pq, n_chunks)
    log.info("log: %s", log_path)
    return 0


def cmd_reembed_all(args) -> int:
    log_path = _configure_logging("index-embed-all")
    log.info("DROPPING all existing question/answer embeddings ...")
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        n = conn.execute("DELETE FROM embeddings WHERE source_type IN ('question','answer')").rowcount
        conn.commit()
        log.info("deleted %d rows; rebuilding ...", n)
    return cmd_embed_questions(args)


def cmd_stats(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        n_q = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
        emb_breakdown = conn.execute(
            """SELECT source_type, COUNT(*) AS chunks,
                      COUNT(DISTINCT source_pq_ref) AS distinct_pqs
                 FROM embeddings
                GROUP BY source_type ORDER BY source_type"""
        ).fetchall()
    print(f"questions:         {n_q}")
    print(f"questions_fts:     {n_fts}")
    print("embeddings:")
    for r in emb_breakdown:
        print(f"  {r['source_type']:15s}  chunks={r['chunks']:>6}  distinct_pqs={r['distinct_pqs']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pq-tracker-index", description="Build & refresh search indexes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rebuild-fts", help="Rebuild the FTS5 questions index.")\
        .set_defaults(func=cmd_rebuild_fts)
    sub.add_parser("embed-questions", help="Compute embeddings for every PQ (incremental).")\
        .set_defaults(func=cmd_embed_questions)
    sub.add_parser("reembed-all", help="Drop and rebuild all question/answer embeddings.")\
        .set_defaults(func=cmd_reembed_all)
    sub.add_parser("stats", help="Show index row counts.")\
        .set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
