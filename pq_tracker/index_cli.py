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


def cmd_embed_questions(args) -> int:
    log_path = _configure_logging("index-embed")
    from . import embeddings as emb
    log.info("embedding questions (model=%s)", emb.MODEL_NAME)
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        existing = emb.existing_signature(conn)
        total = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        log.info("questions in DB: %d  rows already embedded: %d",
                 total, len({k[0] for k in existing}))
        rows = conn.execute(
            "SELECT pq_ref, question_text, answer_text FROM questions ORDER BY pq_ref"
        ).fetchall()
        n_pq = n_chunks = 0
        for i, r in enumerate(rows):
            try:
                added, sources = emb.embed_pq(
                    conn, r["pq_ref"], r["question_text"], r["answer_text"],
                    existing=existing,
                )
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


def cmd_extract_hse_text(args) -> int:
    log_path = _configure_logging("index-hse-text")
    from . import hse_text
    matched_only = not args.all_pdfs
    log.info("extracting HSE PDF text (matched_only=%s, redo=%s, redo_empty=%s, max=%s)",
             matched_only, args.redo, args.redo_empty, args.max)
    has_ocr = hse_text.tesseract_available()
    log.info("Tesseract OCR fallback: %s", "enabled" if has_ocr else "disabled")
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        para_sig = db.hse_paragraph_signature(conn)
        pdf_ids = list(hse_text.iter_pending_pdf_ids(
            conn, matched_only=matched_only, redo=args.redo,
            redo_empty=args.redo_empty,
        ))
        total = len(pdf_ids)
        log.info("queue: %d HSE PDFs to process", total)
        n_done = n_empty = n_failed = n_skipped = n_ocr = 0
        total_paragraphs = 0
        for i, pdf_id in enumerate(pdf_ids, start=1):
            if args.max is not None and (n_done + n_empty + n_failed) >= args.max:
                log.info("hit --max cap, stopping at %d processed", args.max)
                break
            try:
                result = hse_text.extract_and_index(
                    conn, pdf_id, root=cfg.ROOT,
                    existing_para_count=para_sig.get(pdf_id, 0),
                    force=args.redo or args.redo_empty,
                )
            except Exception as e:
                log.warning("unhandled error pdf_id=%d: %s", pdf_id, e)
                n_failed += 1
                continue
            status = result["status"]
            if result.get("ocr_used"):
                n_ocr += 1
            if status == "done":
                if result["skipped"]:
                    n_skipped += 1
                else:
                    n_done += 1
                    total_paragraphs += result["paragraphs"]
            elif status == "empty":
                n_empty += 1
            else:
                n_failed += 1
            if (i % 20) == 0:
                conn.commit()
                log.info("progress: %d/%d (done=%d empty=%d failed=%d skipped=%d ocr=%d paragraphs=%d)",
                         i, total, n_done, n_empty, n_failed, n_skipped, n_ocr, total_paragraphs)
        conn.commit()
    log.info("complete: done=%d empty=%d failed=%d skipped=%d ocr=%d paragraphs=%d",
             n_done, n_empty, n_failed, n_skipped, n_ocr, total_paragraphs)
    log.info("log: %s", log_path)
    print(f"done. log: {log_path}")
    return 0


def cmd_stats(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        n_q = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
        n_paragraphs = conn.execute("SELECT COUNT(*) FROM hse_paragraphs").fetchone()[0]
        n_para_fts = conn.execute("SELECT COUNT(*) FROM hse_paragraphs_fts").fetchone()[0]
        emb_breakdown = conn.execute(
            """SELECT source_type, COUNT(*) AS chunks,
                      COUNT(DISTINCT COALESCE(source_pq_ref, CAST(source_pdf_id AS TEXT))) AS distinct_docs
                 FROM embeddings
                GROUP BY source_type ORDER BY source_type"""
        ).fetchall()
    print(f"questions:           {n_q}")
    print(f"questions_fts:       {n_fts}")
    print(f"hse_paragraphs:      {n_paragraphs}")
    print(f"hse_paragraphs_fts:  {n_para_fts}")
    print("embeddings:")
    for r in emb_breakdown:
        print(f"  {r['source_type']:15s}  chunks={r['chunks']:>6}  distinct_docs={r['distinct_docs']}")
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

    px = sub.add_parser("extract-hse-text",
                        help="Extract paragraph text from downloaded HSE PDFs and "
                             "index them for BM25 + semantic search. Incremental.")
    px.add_argument("--all-pdfs", action="store_true",
                    help="Include PDFs that don't join a row in `questions` "
                         "(default: only PQ-linked PDFs).")
    px.add_argument("--redo", action="store_true",
                    help="Re-extract every PDF, even ones already marked 'done'.")
    px.add_argument("--redo-empty", action="store_true",
                    help="Re-process PDFs marked 'empty' — useful after installing "
                         "Tesseract to OCR previously-skipped image scans.")
    px.add_argument("--max", type=int, default=None,
                    help="Stop after this many PDFs processed (default: all).")
    px.set_defaults(func=cmd_extract_hse_text)

    sub.add_parser("stats", help="Show index row counts.")\
        .set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
