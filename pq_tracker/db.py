from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
  pq_ref               TEXT PRIMARY KEY,
  question_uri         TEXT UNIQUE NOT NULL,
  date_asked           DATE NOT NULL,
  date_answered        DATE,
  td_name              TEXT NOT NULL,
  td_member_code       TEXT NOT NULL,
  td_party             TEXT,
  td_constituency      TEXT,
  department           TEXT NOT NULL,
  minister_name        TEXT,
  question_text        TEXT NOT NULL,
  answer_status        TEXT NOT NULL CHECK (answer_status IN ('pending','answered')),
  matched_topics       TEXT NOT NULL,
  oireachtas_permalink TEXT NOT NULL,
  xml_url              TEXT NOT NULL,
  pdf_url              TEXT,
  hse_pdf_url          TEXT,
  constituent          TEXT,
  notes                TEXT,
  source               TEXT NOT NULL DEFAULT 'oireachtas'
                         CHECK (source IN ('oireachtas','td_email')),
  raw_question_showas  TEXT NOT NULL,
  xml_raw              BLOB,
  first_seen_at        TIMESTAMP NOT NULL,
  last_updated_at      TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_q_date_asked    ON questions(date_asked);
CREATE INDEX IF NOT EXISTS ix_q_status        ON questions(answer_status);
CREATE INDEX IF NOT EXISTS ix_q_member        ON questions(td_member_code);
CREATE INDEX IF NOT EXISTS ix_q_department    ON questions(department);

-- Canonical answer payloads. Many PQs share the same answer text because
-- (a) the minister groups questions ("I propose to take Nos. X and Y together")
-- and (b) HSE re-uses boilerplate answers across years and unrelated PQs.
-- A `content_hash` over whitespace-normalized text deduplicates both cases.
-- `questions.answer_id` is the sole pointer to the payload; readers JOIN
-- through this table for the text.
CREATE TABLE IF NOT EXISTS answers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  content_hash    TEXT NOT NULL UNIQUE,
  answer_text     TEXT NOT NULL,
  first_seen_at   TIMESTAMP NOT NULL,
  last_seen_at    TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_answers_hash ON answers(content_hash);

CREATE TABLE IF NOT EXISTS members_cache (
  member_code         TEXT PRIMARY KEY,
  full_name           TEXT NOT NULL,
  parties_json        TEXT NOT NULL,
  constituencies_json TEXT NOT NULL,
  fetched_at          TIMESTAMP NOT NULL
);

-- Tags per PQ, with three states. Replaces the old `tags` table (which was
-- user-added-only) AND the auto-derived list previously living only in
-- `questions.matched_topics`. The denormalized `matched_topics` JSON is still
-- maintained on `questions` as a fast display cache — the source of truth is
-- this table. State semantics:
--   auto              rule in topics.yaml matched this PQ; included in display
--   user_added        user-added in the modal; included in display, persists
--                     across topics.yaml edits and rebuilds
--   user_suppressed   user removed an auto tag; the rule may still match but
--                     we don't re-add it on rebuild. NOT shown in display.
-- Modal flow: backend takes the desired display list, classifies each entry
-- as auto (if rule matches) or user_added (if not), and stores
-- (auto-rule-matches - desired) as user_suppressed.
CREATE TABLE IF NOT EXISTS pq_tags (
  pq_ref TEXT NOT NULL,
  tag    TEXT NOT NULL,
  state  TEXT NOT NULL CHECK (state IN ('auto','user_added','user_suppressed')),
  PRIMARY KEY (pq_ref, tag),
  FOREIGN KEY (pq_ref) REFERENCES questions(pq_ref) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_pq_tags_tag   ON pq_tags(tag);
CREATE INDEX IF NOT EXISTS ix_pq_tags_state ON pq_tags(state);

CREATE TABLE IF NOT EXISTS run_log (
  run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at        TIMESTAMP NOT NULL,
  finished_at       TIMESTAMP,
  date_start_param  DATE NOT NULL,
  date_end_param    DATE NOT NULL,
  topics_snapshot   TEXT NOT NULL,
  new_questions     INTEGER NOT NULL DEFAULT 0,
  newly_answered    INTEGER NOT NULL DEFAULT 0,
  errors_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hse_pdfs (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  source                   TEXT NOT NULL CHECK (source IN ('hse_live','hse_wayback')),
  source_url               TEXT NOT NULL UNIQUE,
  index_url                TEXT,
  filename                 TEXT NOT NULL,
  local_path               TEXT,
  pq_refs_json             TEXT NOT NULL DEFAULT '[]',
  publication_date         DATE,
  sha256                   TEXT,
  bytes                    INTEGER,
  fetched_at               TIMESTAMP,
  first_seen_at            TIMESTAMP NOT NULL,
  text_extraction_status   TEXT,   -- NULL=not tried, 'done', 'empty', 'failed'
  text_extracted_at        TIMESTAMP,
  text_page_count          INTEGER,
  ocr_used                 INTEGER NOT NULL DEFAULT 0   -- 0/1 — Tesseract OCR fallback was applied
);
CREATE INDEX IF NOT EXISTS ix_hse_pdfs_filename ON hse_pdfs(filename);
-- ix_hse_pdfs_extract is created in init_schema() after the ALTER-ADD-COLUMN
-- migration, since existing installs gain the column post-executescript().

CREATE TABLE IF NOT EXISTS hse_pdf_pqs (
  hse_pdf_id INTEGER NOT NULL,
  pq_ref     TEXT NOT NULL,
  PRIMARY KEY (hse_pdf_id, pq_ref),
  FOREIGN KEY (hse_pdf_id) REFERENCES hse_pdfs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_hse_pdf_pqs_ref ON hse_pdf_pqs(pq_ref);

-- One row per paragraph extracted from an HSE PDF. Source of truth for
-- both lexical (hse_paragraphs_fts) and semantic (embeddings WHERE
-- source_type='hse_paragraph', joined via source_pdf_id+chunk_index→para_index)
-- search over PDF text. Rebuildable from local_path on disk.
CREATE TABLE IF NOT EXISTS hse_paragraphs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  hse_pdf_id  INTEGER NOT NULL,
  page_no     INTEGER NOT NULL,
  para_index  INTEGER NOT NULL,
  text        TEXT NOT NULL,
  FOREIGN KEY (hse_pdf_id) REFERENCES hse_pdfs(id) ON DELETE CASCADE,
  UNIQUE (hse_pdf_id, page_no, para_index)
);
CREATE INDEX IF NOT EXISTS ix_hse_paragraphs_pdf ON hse_paragraphs(hse_pdf_id);

-- FTS5 mirror over hse_paragraphs.text. Same tokenizer settings as
-- questions_fts so behaviour matches (stemmed, diacritic-folded, wildcards).
CREATE VIRTUAL TABLE IF NOT EXISTS hse_paragraphs_fts USING fts5(
  text,
  content='hse_paragraphs',
  content_rowid='id',
  tokenize='porter unicode61 remove_diacritics 2'
);

-- FTS5 for BM25/lexical search over the questions table. Porter stemmer over
-- a Unicode case-folding tokenizer: "diabetes" ↔ "diabetic", phrase queries
-- "home help hours" match stems regardless of conjugation, wildcards via diab*.
-- Inline-content (FTS owns its own copy): answer_text used to live on
-- `questions` directly; it now lives on `answers` and is joined in at write
-- time via `refresh_fts_for_pq`. Inline content (vs `content='questions'`) is
-- required because we use highlight() and snippet() in the modal/search paths,
-- which contentless FTS5 doesn't support. `pq_ref` is UNINDEXED so callers can
-- JOIN back to `questions` without a rowid coupling — keeps the index stable
-- across deletes/inserts. Rebuild after bulk text changes via
-- `python -m pq_tracker.index_cli rebuild-fts`.
CREATE VIRTUAL TABLE IF NOT EXISTS questions_fts USING fts5(
  question_text, answer_text,
  pq_ref UNINDEXED,
  tokenize='porter unicode61 remove_diacritics 2'
);

-- Derived vector embeddings. Generic over source types so we can later add
-- HSE paragraph chunks without schema churn. Vectors stored as packed float32
-- BLOBs. Rebuildable from the source text in `questions` / `hse_paragraphs`,
-- so we can swap models or change chunking later.
CREATE TABLE IF NOT EXISTS embeddings (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type     TEXT NOT NULL CHECK (source_type IN ('question','answer','hse_paragraph')),
  source_pq_ref   TEXT,
  source_pdf_id   INTEGER,
  chunk_index     INTEGER NOT NULL DEFAULT 0,
  text_excerpt    TEXT NOT NULL,
  model           TEXT NOT NULL,
  dims            INTEGER NOT NULL,
  vector          BLOB NOT NULL,
  fetched_at      TIMESTAMP NOT NULL,
  FOREIGN KEY (source_pq_ref) REFERENCES questions(pq_ref) ON DELETE CASCADE,
  FOREIGN KEY (source_pdf_id) REFERENCES hse_pdfs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_emb_pq_ref ON embeddings(source_pq_ref);
CREATE INDEX IF NOT EXISTS ix_emb_pdf_id ON embeddings(source_pdf_id);
CREATE INDEX IF NOT EXISTS ix_emb_type   ON embeddings(source_type);
"""


@contextmanager
def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 30s if another writer holds the lock instead of failing
    # immediately — important when ingests, indexers, and the UI race.
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _answer_content_hash(answer_text: str) -> str:
    """Normalize whitespace then sha256-hex. Used to dedupe answer payloads."""
    return hashlib.sha256(" ".join(answer_text.split()).encode("utf-8")).hexdigest()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Lightweight migration for installs created before a column existed.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
    if "constituent" not in cols:
        conn.execute("ALTER TABLE questions ADD COLUMN constituent TEXT")
    if "notes" not in cols:
        conn.execute("ALTER TABLE questions ADD COLUMN notes TEXT")
    if "xml_raw" not in cols:
        # Stores the raw Akoma Ntoso XML for each PQ so the modal can render
        # nicely-formatted HTML without round-tripping through plain text.
        conn.execute("ALTER TABLE questions ADD COLUMN xml_raw BLOB")
    if "answer_id" not in cols:
        # Canonical pointer into `answers`. Backfill below populates it for
        # every answered row; ingest paths going forward set it inline.
        conn.execute("ALTER TABLE questions ADD COLUMN answer_id INTEGER "
                     "REFERENCES answers(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_q_answer_id "
                     "  ON questions(answer_id)")
    if "question_group_key" not in cols:
        # Deterministic key shared by every member of a minister-grouped
        # question set. NULL for singletons and for the rare eId-mismatch
        # rows. Backfilled below from xml_raw; ingest path keeps it fresh.
        conn.execute("ALTER TABLE questions ADD COLUMN question_group_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_q_group_key "
                     "  ON questions(question_group_key)")
    # Backfill answers/answer_id from legacy `answer_text` column when it
    # still exists (pre-2026-05-17 schema). Each run hashes every answered
    # row missing an answer_id, upserts into `answers`, and points back.
    # Skipped wholesale on post-DROP-COLUMN installs.
    if "answer_text" in cols:
        pending = conn.execute(
            """SELECT pq_ref, answer_text FROM questions
                WHERE answer_status = 'answered'
                  AND answer_text IS NOT NULL
                  AND length(trim(answer_text)) > 0
                  AND answer_id IS NULL"""
        ).fetchall()
        if pending:
            now = datetime.utcnow().isoformat(timespec="seconds")
            for r in pending:
                h = _answer_content_hash(r["answer_text"])
                existing = conn.execute(
                    "SELECT id FROM answers WHERE content_hash = ?", (h,)
                ).fetchone()
                if existing:
                    ans_id = existing["id"]
                    conn.execute(
                        "UPDATE answers SET last_seen_at = ? WHERE id = ?",
                        (now, ans_id),
                    )
                else:
                    cur = conn.execute(
                        "INSERT INTO answers(content_hash, answer_text, "
                        "  first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                        (h, r["answer_text"], now, now),
                    )
                    ans_id = cur.lastrowid
                conn.execute(
                    "UPDATE questions SET answer_id = ? WHERE pq_ref = ?",
                    (ans_id, r["pq_ref"]),
                )
    # Backfill question_group_key for any row whose xml_raw exists but the
    # column wasn't yet populated (post-migration, or after the ingest path
    # changed). Idempotent — only touches rows where the computed key differs
    # from what's stored, and skips rows without xml_raw.
    pending_keys = conn.execute(
        """SELECT pq_ref, date_asked, question_uri, xml_raw, question_group_key
             FROM questions
            WHERE xml_raw IS NOT NULL AND xml_raw != ''
              AND question_group_key IS NULL"""
    ).fetchall()
    for r in pending_keys:
        my_eid = (r["question_uri"] or "").rsplit("/", 1)[-1]
        key = _group_key_for(r["xml_raw"], r["date_asked"], my_eid)
        if key != r["question_group_key"]:
            conn.execute(
                "UPDATE questions SET question_group_key = ? WHERE pq_ref = ?",
                (key, r["pq_ref"]),
            )
    hse_cols = {row[1] for row in conn.execute("PRAGMA table_info(hse_pdfs)")}
    if "text_extraction_status" not in hse_cols:
        conn.execute("ALTER TABLE hse_pdfs ADD COLUMN text_extraction_status TEXT")
    if "text_extracted_at" not in hse_cols:
        conn.execute("ALTER TABLE hse_pdfs ADD COLUMN text_extracted_at TIMESTAMP")
    if "text_page_count" not in hse_cols:
        conn.execute("ALTER TABLE hse_pdfs ADD COLUMN text_page_count INTEGER")
    if "ocr_used" not in hse_cols:
        conn.execute("ALTER TABLE hse_pdfs ADD COLUMN ocr_used INTEGER NOT NULL DEFAULT 0")
    # Now that the new columns exist on every install, the index is safe.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_hse_pdfs_extract "
        "  ON hse_pdfs(text_extraction_status)"
    )
    # One-shot data migration: strip the dead `/pq_` prefix from permalinks.
    # Cause: API gives e_id like "pq_1000" but the public site only accepts the
    # numeric tail (`.../question/2026-03-24/1000/`, not `.../pq_1000/`).
    # Idempotent — once the first run touches every affected row, the WHERE
    # filters out everything, so subsequent app starts are a no-op.
    conn.execute(
        """UPDATE questions
              SET oireachtas_permalink =
                  replace(oireachtas_permalink, '/pq_', '/')
            WHERE oireachtas_permalink LIKE '%/pq_%'"""
    )
    # FTS5 shape migration: old installs have `questions_fts` declared as
    # `content='questions'` over (question_text, answer_text). New shape is
    # inline-content over (question_text, answer_text, pq_ref UNINDEXED).
    # Detect via the stored CREATE statement and rebuild from a JOIN.
    fts_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='questions_fts'"
    ).fetchone()
    fts_sql = (fts_sql_row[0] if fts_sql_row else "") or ""
    if "content='questions'" in fts_sql or "content=questions" in fts_sql:
        conn.execute("DROP TABLE questions_fts")
        conn.execute(
            """CREATE VIRTUAL TABLE questions_fts USING fts5(
                 question_text, answer_text,
                 pq_ref UNINDEXED,
                 tokenize='porter unicode61 remove_diacritics 2'
               )"""
        )
        # Populate from JOIN through `answers`. Fall back to the legacy
        # `questions.answer_text` column when it still exists and `answer_id`
        # hasn't been backfilled (shouldn't happen post-backfill above, but
        # COALESCE keeps the FTS coverage complete just in case).
        if "answer_text" in cols:
            conn.execute(
                """INSERT INTO questions_fts(pq_ref, question_text, answer_text)
                   SELECT q.pq_ref, q.question_text,
                          COALESCE(a.answer_text, q.answer_text)
                     FROM questions q
                     LEFT JOIN answers a ON a.id = q.answer_id"""
            )
        else:
            conn.execute(
                """INSERT INTO questions_fts(pq_ref, question_text, answer_text)
                   SELECT q.pq_ref, q.question_text, a.answer_text
                     FROM questions q
                     LEFT JOIN answers a ON a.id = q.answer_id"""
            )
    # DROP the legacy `questions.answer_text` column. The canonical store is
    # now `answers(answer_text)` joined via `questions.answer_id`. Idempotent —
    # SQLite 3.35+ supports DROP COLUMN; once dropped, the check above no-ops.
    if "answer_text" in cols:
        conn.execute("ALTER TABLE questions DROP COLUMN answer_text")
    # Tags model migration: collapse the old `tags` table (user-added only) and
    # the auto-derived list-on-`matched_topics` into a single `pq_tags` table
    # with explicit state. Old `tags` rows become state='user_added'; existing
    # `matched_topics` JSON becomes state='auto' (one row per element).
    # Idempotent — `tags` only exists pre-migration; the auto-backfill skips
    # rows that already have any pq_tags entries.
    has_old_tags = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tags'"
    ).fetchone() is not None
    if has_old_tags:
        conn.execute(
            """INSERT OR IGNORE INTO pq_tags(pq_ref, tag, state)
               SELECT pq_ref, tag, 'user_added' FROM tags"""
        )
        conn.execute("DROP TABLE tags")
    # Auto-state backfill from existing matched_topics JSON. Only touch PQs
    # that have no pq_tags rows yet — second runs are no-ops.
    has_any = conn.execute("SELECT 1 FROM pq_tags LIMIT 1").fetchone()
    if not has_any:
        for r in conn.execute(
            "SELECT pq_ref, matched_topics FROM questions WHERE matched_topics IS NOT NULL"
        ).fetchall():
            try:
                topics = json.loads(r["matched_topics"] or "[]")
            except json.JSONDecodeError:
                continue
            if not topics:
                continue
            conn.executemany(
                "INSERT OR IGNORE INTO pq_tags(pq_ref, tag, state) VALUES (?, ?, 'auto')",
                [(r["pq_ref"], t) for t in topics if t],
            )
    conn.commit()


def question_status(conn: sqlite3.Connection, pq_ref: str) -> str | None:
    """Return existing answer_status for a pq_ref, or None if unknown.

    Used by ingest to short-circuit XML fetches for PQs we've already fully
    captured (resumability + cheap incremental runs over historical data).
    """
    row = conn.execute(
        "SELECT answer_status FROM questions WHERE pq_ref = ?", (pq_ref,)
    ).fetchone()
    return row["answer_status"] if row else None


def get_cached_member(conn: sqlite3.Connection, member_code: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM members_cache WHERE member_code = ?", (member_code,)
    ).fetchone()
    return row


def upsert_member(conn: sqlite3.Connection, member_code: str, full_name: str,
                  parties_json: str, constituencies_json: str) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO members_cache(member_code, full_name, parties_json, constituencies_json, fetched_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(member_code) DO UPDATE SET
          full_name=excluded.full_name,
          parties_json=excluded.parties_json,
          constituencies_json=excluded.constituencies_json,
          fetched_at=excluded.fetched_at
        """,
        (member_code, full_name, parties_json, constituencies_json, now),
    )


def refresh_fts_for_pq(conn: sqlite3.Connection, pq_ref: str,
                       question_text: str | None,
                       answer_text: str | None) -> None:
    """Mirror current question+answer text into the inline-content FTS5 index.

    Inline-content FTS5 doesn't auto-sync from the source tables (that was the
    old `content='questions'` behaviour). Every code path that writes
    question text or a new/changed answer must call this so search stays
    correct. Delete-then-insert keeps the row count == question count.

    NULL answer_text → store empty string (FTS5 won't index it but the row
    must exist so the question_text side is searchable).
    """
    conn.execute("DELETE FROM questions_fts WHERE pq_ref = ?", (pq_ref,))
    conn.execute(
        """INSERT INTO questions_fts(pq_ref, question_text, answer_text)
           VALUES (?, ?, ?)""",
        (pq_ref, question_text or "", answer_text or ""),
    )


def upsert_answer(conn: sqlite3.Connection, answer_text: str | None) -> int | None:
    """Return the canonical answers.id for this payload. Idempotent on content hash.

    None/empty input → None (pending PQs have no answer to canonicalize).
    """
    if not answer_text or not answer_text.strip():
        return None
    h = _answer_content_hash(answer_text)
    now = datetime.utcnow().isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT id FROM answers WHERE content_hash = ?", (h,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE answers SET last_seen_at = ? WHERE id = ?",
            (now, existing["id"]),
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO answers(content_hash, answer_text, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (h, answer_text, now, now),
    )
    return cur.lastrowid


def upsert_question(conn: sqlite3.Connection, *,
                    pq_ref: str,
                    question_uri: str,
                    date_asked,
                    date_answered,
                    td_name: str,
                    td_member_code: str,
                    td_party: str | None,
                    td_constituency: str | None,
                    department: str,
                    minister_name: str | None,
                    question_text: str,
                    answer_text: str | None,
                    answer_status: str,
                    oireachtas_permalink: str,
                    xml_url: str,
                    pdf_url: str | None,
                    raw_question_showas: str,
                    xml_raw: bytes | None = None) -> dict:
    """Returns {'inserted': bool, 'newly_answered': bool}.

    ``matched_topics`` is no longer a param — the source of truth is the
    ``pq_tags`` table. Callers must call ``apply_auto_tags`` after this to
    persist freshly-computed auto labels and refresh the matched_topics cache.
    On INSERT, matched_topics is initialised to '[]'; UPDATE leaves it alone.
    """
    now = datetime.utcnow().isoformat(timespec="seconds")
    answer_id = upsert_answer(conn, answer_text)
    existing = conn.execute(
        "SELECT pq_ref, answer_status FROM questions WHERE pq_ref = ?", (pq_ref,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO questions(
              pq_ref, question_uri, date_asked, date_answered,
              td_name, td_member_code, td_party, td_constituency,
              department, minister_name,
              question_text, answer_id, answer_status,
              matched_topics, oireachtas_permalink,
              xml_url, pdf_url, hse_pdf_url, constituent, notes,
              source, raw_question_showas, xml_raw,
              first_seen_at, last_updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',?,?,?,NULL,NULL,NULL,'oireachtas',?,?,?,?)
            """,
            (
                pq_ref, question_uri,
                date_asked.isoformat() if date_asked else None,
                date_answered.isoformat() if date_answered else None,
                td_name, td_member_code, td_party, td_constituency,
                department, minister_name,
                question_text, answer_id, answer_status,
                oireachtas_permalink,
                xml_url, pdf_url,
                raw_question_showas, xml_raw,
                now, now,
            ),
        )
        refresh_fts_for_pq(conn, pq_ref, question_text, answer_text)
        return {"inserted": True, "newly_answered": answer_status == "answered"}
    # Update existing row. Preserve hse_pdf_url (manual attachment), first_seen_at,
    # and matched_topics (managed by apply_auto_tags).
    was_pending = existing["answer_status"] == "pending"
    # xml_raw: preserve existing when caller didn't pass new bytes (e.g. a
    # pending→pending re-scan with no XML refetch). Overwrite when caller did.
    if xml_raw is not None:
        conn.execute(
            """
            UPDATE questions SET
              question_uri = ?,
              date_asked = ?,
              date_answered = ?,
              td_name = ?,
              td_member_code = ?,
              td_party = ?,
              td_constituency = ?,
              department = ?,
              minister_name = ?,
              question_text = ?,
              answer_id = ?,
              answer_status = ?,
              oireachtas_permalink = ?,
              xml_url = ?,
              pdf_url = ?,
              raw_question_showas = ?,
              xml_raw = ?,
              last_updated_at = ?
            WHERE pq_ref = ?
            """,
            (
                question_uri,
                date_asked.isoformat() if date_asked else None,
                date_answered.isoformat() if date_answered else None,
                td_name, td_member_code, td_party, td_constituency,
                department, minister_name,
                question_text, answer_id, answer_status,
                oireachtas_permalink, xml_url, pdf_url,
                raw_question_showas, xml_raw,
                now, pq_ref,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE questions SET
              question_uri = ?,
              date_asked = ?,
              date_answered = ?,
              td_name = ?,
              td_member_code = ?,
              td_party = ?,
              td_constituency = ?,
              department = ?,
              minister_name = ?,
              question_text = ?,
              answer_id = ?,
              answer_status = ?,
              oireachtas_permalink = ?,
              xml_url = ?,
              pdf_url = ?,
              raw_question_showas = ?,
              last_updated_at = ?
            WHERE pq_ref = ?
            """,
            (
                question_uri,
                date_asked.isoformat() if date_asked else None,
                date_answered.isoformat() if date_answered else None,
                td_name, td_member_code, td_party, td_constituency,
                department, minister_name,
                question_text, answer_id, answer_status,
                oireachtas_permalink, xml_url, pdf_url,
                raw_question_showas,
                now, pq_ref,
            ),
        )
    refresh_fts_for_pq(conn, pq_ref, question_text, answer_text)
    return {"inserted": False, "newly_answered": was_pending and answer_status == "answered"}


def start_run(conn: sqlite3.Connection, date_start, date_end, topics_snapshot) -> int:
    cur = conn.execute(
        """INSERT INTO run_log(started_at, date_start_param, date_end_param, topics_snapshot)
           VALUES (?,?,?,?)""",
        (datetime.utcnow().isoformat(timespec="seconds"),
         date_start.isoformat(), date_end.isoformat(),
         json.dumps(topics_snapshot, ensure_ascii=False)),
    )
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, new_questions: int,
               newly_answered: int, errors_count: int) -> None:
    conn.execute(
        """UPDATE run_log SET finished_at=?, new_questions=?, newly_answered=?, errors_count=?
           WHERE run_id=?""",
        (datetime.utcnow().isoformat(timespec="seconds"),
         new_questions, newly_answered, errors_count, run_id),
    )


def all_matches_in_run(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM questions WHERE last_updated_at >= ? ORDER BY date_asked DESC""",
        (since_iso,),
    ).fetchall()


def all_questions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM questions ORDER BY date_asked DESC, pq_ref").fetchall()


def update_manual_fields(conn: sqlite3.Connection, pq_ref: str,
                         constituent: str | None, notes: str | None,
                         hse_pdf_url: str | None) -> bool:
    """Update only the user-editable fields. Returns True if a row was updated."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    cur = conn.execute(
        """UPDATE questions
              SET constituent = ?, notes = ?, hse_pdf_url = ?, last_updated_at = ?
            WHERE pq_ref = ?""",
        (constituent, notes, hse_pdf_url, now, pq_ref),
    )
    return cur.rowcount > 0


def get_question(conn: sqlite3.Connection, pq_ref: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM questions WHERE pq_ref = ?", (pq_ref,)).fetchone()


_Q_EID_RE = re.compile(r'<question\s+[^>]*?eId="([^"]+)"')


def _group_key_for(xml_raw: bytes | str | None, date_asked: str,
                   my_eid: str) -> str | None:
    """Deterministic key shared by every member of a minister-grouped question.

    Returns ``None`` when the PQ is a singleton (no XML, only one ``<question>``
    eId, or the eId-mismatch case where our own eId isn't in the XML's set).

    Format: ``"<date_asked>|pq_NNN,pq_MMM,..."`` with eIds sorted lexicographically.
    Every group member computes the same key independently from its own
    ``xml_raw`` — no rowid coupling, so the key is stable across prunes /
    re-inserts.
    """
    if not xml_raw:
        return None
    if isinstance(xml_raw, (bytes, bytearray)):
        xml_text = bytes(xml_raw).decode("utf-8", errors="replace")
    else:
        xml_text = xml_raw
    eids = _Q_EID_RE.findall(xml_text)
    if len(eids) <= 1:
        return None
    if my_eid not in eids:
        return None
    return f"{date_asked}|" + ",".join(sorted(eids))


def update_group_key_for_pq(conn: sqlite3.Connection, pq_ref: str) -> str | None:
    """Recompute & persist ``question_group_key`` for one PQ. Returns the new key."""
    row = conn.execute(
        "SELECT date_asked, question_uri, xml_raw FROM questions WHERE pq_ref = ?",
        (pq_ref,),
    ).fetchone()
    if row is None:
        return None
    my_eid = (row["question_uri"] or "").rsplit("/", 1)[-1]
    key = _group_key_for(row["xml_raw"], row["date_asked"], my_eid)
    conn.execute(
        "UPDATE questions SET question_group_key = ? WHERE pq_ref = ?",
        (key, pq_ref),
    )
    return key


def get_group_siblings(conn: sqlite3.Connection, pq_ref: str) -> dict:
    """Return the minister-grouped siblings for this PQ, derived from xml_raw.

    The Oireachtas XML for a grouped PQ already contains one `<question>`
    element per group member before the shared `<speech>`. We extract those
    eIds, map each to (date_asked, eId) → pq_ref in our corpus, and return
    a mixed list:

      - in_corpus=True siblings have pq_ref + td_name (clickable in-app)
      - in_corpus=False siblings have only e_id + oireachtas_url (out-link)

    Off-corpus siblings exist when the minister grouped a question that
    didn't pass our search_terms gate at ingest time. Surfacing them lets
    you click through to Oireachtas without giving the false impression
    that the group is smaller than it really is.

    Returns: {"siblings": list[dict], "total": int}.
    `total` includes this PQ itself; `siblings` excludes it.
    A non-grouped PQ returns {"siblings": [], "total": 1}.
    """
    row = conn.execute(
        "SELECT xml_raw, date_asked, question_uri FROM questions WHERE pq_ref = ?",
        (pq_ref,),
    ).fetchone()
    if row is None or not row["xml_raw"]:
        return {"siblings": [], "total": 1}
    xml = row["xml_raw"]
    if isinstance(xml, (bytes, bytearray)):
        xml = bytes(xml).decode("utf-8", errors="replace")
    eids = _Q_EID_RE.findall(xml)
    if len(eids) <= 1:
        return {"siblings": [], "total": 1}
    my_eid = (row["question_uri"] or "").rsplit("/", 1)[-1]
    if my_eid not in eids:
        # Known Oireachtas API quirk: a handful of rows have an xml_raw whose
        # <question> eIds don't include this row's own eId — i.e. the stored
        # XML is the response to a different question. Without our own eId in
        # the set we can't safely claim those other eIds as our siblings, so
        # bail out as ungrouped. Memory notes this affects ~0.08% of rows.
        return {"siblings": [], "total": 1}
    date_asked = row["date_asked"]
    out: list[dict] = []
    for eid in eids:
        if eid == my_eid:
            continue
        # Strip the "pq_" prefix to get the agenda number used in web URLs.
        agenda_no = eid[3:] if eid.startswith("pq_") else eid
        web_url = (f"https://www.oireachtas.ie/en/debates/question/"
                   f"{date_asked}/{agenda_no}/")
        sib = conn.execute(
            "SELECT pq_ref, td_name FROM questions "
            "WHERE date_asked = ? AND question_uri LIKE ?",
            (date_asked, f"%/{eid}"),
        ).fetchone()
        if sib is not None:
            out.append({
                "in_corpus": True,
                "pq_ref": sib["pq_ref"],
                "e_id": eid,
                "date_asked": date_asked,
                "td_name": sib["td_name"],
                "oireachtas_url": web_url,
            })
        else:
            out.append({
                "in_corpus": False,
                "pq_ref": None,
                "e_id": eid,
                "date_asked": date_asked,
                "td_name": None,
                "oireachtas_url": web_url,
            })
    return {"siblings": out, "total": len(eids)}


def get_pq_tags(conn: sqlite3.Connection, pq_ref: str) -> list[dict]:
    """Display set for a PQ: (auto + user_added) rows from pq_tags.

    Returns ``[{tag, state}, ...]`` so the modal can render per-chip styling.
    user_suppressed rows are excluded — they only matter to the rebuild path.
    """
    rows = conn.execute(
        """SELECT tag, state FROM pq_tags
            WHERE pq_ref = ? AND state IN ('auto', 'user_added')
            ORDER BY LOWER(tag)""",
        (pq_ref,),
    ).fetchall()
    return [{"tag": r["tag"], "state": r["state"]} for r in rows]


def _normalize_tag_list(tags: list[str]) -> list[str]:
    """Strip, drop empty, dedup case-insensitively preserving first-seen case."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        s = (t or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _refresh_matched_topics(conn: sqlite3.Connection, pq_ref: str) -> list[str]:
    """Recompute ``questions.matched_topics`` JSON from current pq_tags rows.
    Returns the new display list."""
    rows = conn.execute(
        """SELECT tag FROM pq_tags
            WHERE pq_ref = ? AND state IN ('auto', 'user_added')
            ORDER BY LOWER(tag)""",
        (pq_ref,),
    ).fetchall()
    display = [r["tag"] for r in rows]
    conn.execute(
        "UPDATE questions SET matched_topics = ? WHERE pq_ref = ?",
        (json.dumps(display, ensure_ascii=False), pq_ref),
    )
    return display


def set_desired_tags(conn: sqlite3.Connection, pq_ref: str,
                     desired: list[str], auto_labels: list[str]) -> list[dict]:
    """Persist the user's desired display set for one PQ.

    Each desired tag becomes:
      - state='auto'        if it matches an entry in ``auto_labels`` (i.e.
                              the current keyword rule would derive it for
                              this PQ's text)
      - state='user_added'  otherwise

    Auto labels NOT in ``desired`` become state='user_suppressed' — that's the
    user's intent that the rebuild must respect.

    Wipes existing pq_tags rows for this PQ and reinserts. Refreshes the
    denormalized ``questions.matched_topics`` cache. Returns the resulting
    display rows.
    """
    desired_clean = _normalize_tag_list(desired)
    auto_clean = _normalize_tag_list(auto_labels)

    # Build a case-insensitive index of auto_labels for membership checks
    # while preserving the desired tag's case in storage.
    auto_lc = {a.lower(): a for a in auto_clean}
    desired_lc = {t.lower() for t in desired_clean}

    new_rows: list[tuple[str, str]] = []
    for t in desired_clean:
        if t.lower() in auto_lc:
            new_rows.append((t, "auto"))
        else:
            new_rows.append((t, "user_added"))
    for a in auto_clean:
        if a.lower() not in desired_lc:
            new_rows.append((a, "user_suppressed"))

    conn.execute("DELETE FROM pq_tags WHERE pq_ref = ?", (pq_ref,))
    if new_rows:
        conn.executemany(
            "INSERT INTO pq_tags(pq_ref, tag, state) VALUES (?, ?, ?)",
            [(pq_ref, t, s) for (t, s) in new_rows],
        )
    _refresh_matched_topics(conn, pq_ref)
    return [{"tag": t, "state": s} for (t, s) in new_rows if s != "user_suppressed"]


def apply_auto_tags(conn: sqlite3.Connection, pq_ref: str,
                    new_auto: list[str]) -> list[str]:
    """Apply a freshly-derived auto-label set to one PQ while preserving user
    overrides. Used by ingest (after upsert) and by the topics-rebuild loop.

    Behaviour:
      - For each tag in ``new_auto``: if a user_suppressed row exists, leave it
        suppressed; otherwise ensure an 'auto' row exists.
      - Existing 'auto' rows whose tag is NOT in ``new_auto`` are dropped
        (the rule no longer matches; nothing else to anchor them).
      - 'user_added' and 'user_suppressed' rows are never touched.

    Refreshes matched_topics. Returns the new display list.
    """
    new_auto_clean = _normalize_tag_list(new_auto)
    existing = {
        r["tag"]: r["state"]
        for r in conn.execute(
            "SELECT tag, state FROM pq_tags WHERE pq_ref = ?", (pq_ref,)
        )
    }
    target: dict[str, str] = {}
    for t in new_auto_clean:
        prior = existing.get(t)
        if prior == "user_suppressed":
            target[t] = "user_suppressed"
        else:
            target[t] = "auto"
    for tag, state in existing.items():
        if tag in target:
            continue
        if state in ("user_added", "user_suppressed"):
            target[tag] = state
        # state='auto' and not in new_auto → drop (don't copy into target)

    conn.execute("DELETE FROM pq_tags WHERE pq_ref = ?", (pq_ref,))
    if target:
        conn.executemany(
            "INSERT INTO pq_tags(pq_ref, tag, state) VALUES (?, ?, ?)",
            [(pq_ref, t, s) for (t, s) in target.items()],
        )
    return _refresh_matched_topics(conn, pq_ref)


def all_tags(conn: sqlite3.Connection) -> list[str]:
    """Distinct tags currently displayed somewhere (state IN auto/user_added),
    sorted case-insensitively. Powers modal autocomplete."""
    rows = conn.execute(
        """SELECT DISTINCT tag FROM pq_tags
            WHERE state IN ('auto', 'user_added')
            ORDER BY LOWER(tag)"""
    ).fetchall()
    return [r[0] for r in rows]


def tag_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """For the /settings tag admin. Counts the *displayed* presence of each tag
    (auto + user_added), regardless of source, sorted by count desc."""
    rows = conn.execute(
        """SELECT tag, COUNT(*) AS n FROM pq_tags
            WHERE state IN ('auto', 'user_added')
            GROUP BY tag ORDER BY n DESC, LOWER(tag)"""
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def rename_tag(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Rename a tag across every PQ that has it. Only touches state='user_added'
    rows — renaming an 'auto' tag here would just be reverted on the next
    rebuild, so we leave those alone and let the user rename the keyword in
    topics.yaml instead. Returns the number of PQs affected."""
    new = (new or "").strip()
    old = (old or "").strip()
    if not new or not old or new.lower() == old.lower():
        return 0
    affected = [r[0] for r in conn.execute(
        "SELECT pq_ref FROM pq_tags WHERE tag = ? AND state = 'user_added'", (old,)
    )]
    if not affected:
        return 0
    # Insert new where missing, then drop old. PK collisions silently ignored.
    conn.executemany(
        "INSERT OR IGNORE INTO pq_tags(pq_ref, tag, state) VALUES (?, ?, 'user_added')",
        [(p, new) for p in affected],
    )
    conn.execute("DELETE FROM pq_tags WHERE tag = ? AND state = 'user_added'", (old,))
    for p in affected:
        _refresh_matched_topics(conn, p)
    return len(affected)


def delete_tag(conn: sqlite3.Connection, tag: str) -> int:
    """Remove a user_added tag everywhere. Auto tags are left intact (use the
    keyword settings to remove those). Returns rows deleted."""
    tag = (tag or "").strip()
    if not tag:
        return 0
    affected = [r[0] for r in conn.execute(
        "SELECT pq_ref FROM pq_tags WHERE tag = ? AND state = 'user_added'", (tag,)
    )]
    cur = conn.execute(
        "DELETE FROM pq_tags WHERE tag = ? AND state = 'user_added'", (tag,)
    )
    for p in affected:
        _refresh_matched_topics(conn, p)
    return cur.rowcount


def tags_by_pqref(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Bulk-fetch the display tag set grouped by pq_ref. For exports."""
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        """SELECT pq_ref, tag FROM pq_tags
            WHERE state IN ('auto', 'user_added')
            ORDER BY pq_ref, LOWER(tag)"""
    ):
        out.setdefault(r[0], []).append(r[1])
    return out


def pq_tags_by_pqref(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Bulk-fetch the display rows (tag + state) per pq_ref. Used by the
    list view to colour chips state-aware without per-row queries."""
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
        """SELECT pq_ref, tag, state FROM pq_tags
            WHERE state IN ('auto', 'user_added')
            ORDER BY pq_ref, LOWER(tag)"""
    ):
        out.setdefault(r[0], []).append({"tag": r[1], "state": r[2]})
    return out


def user_tags_by_pqref(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Bulk-fetch only the user_added subset, for the 'Tags (manual)' xlsx column."""
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        """SELECT pq_ref, tag FROM pq_tags
            WHERE state = 'user_added'
            ORDER BY pq_ref, LOWER(tag)"""
    ):
        out.setdefault(r[0], []).append(r[1])
    return out


def answers_by_pqref(conn: sqlite3.Connection) -> dict[str, str]:
    """Bulk-fetch answer text for every PQ that has one. JOIN-once pattern for
    exports that would otherwise lookup `answers` per row."""
    rows = conn.execute(
        """SELECT q.pq_ref, a.answer_text
             FROM questions q
             JOIN answers a ON a.id = q.answer_id
            WHERE q.answer_id IS NOT NULL"""
    ).fetchall()
    return {r["pq_ref"]: r["answer_text"] for r in rows}


def upsert_hse_pdf(conn: sqlite3.Connection, *,
                   source: str,
                   source_url: str,
                   index_url: str | None,
                   filename: str,
                   pq_refs: list[str],
                   publication_date: str | None,
                   local_path: str | None = None,
                   sha256: str | None = None,
                   bytes_size: int | None = None,
                   fetched_at: str | None = None) -> tuple[int, bool]:
    """Insert or update an hse_pdfs row keyed on source_url. Syncs the junction.

    Returns (row_id, inserted) where inserted is True on a fresh insert.
    """
    now = datetime.utcnow().isoformat(timespec="seconds")
    row = conn.execute("SELECT id FROM hse_pdfs WHERE source_url = ?", (source_url,)).fetchone()
    refs_json = json.dumps(pq_refs, ensure_ascii=False)
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO hse_pdfs(source, source_url, index_url, filename,
                                 local_path, pq_refs_json, publication_date,
                                 sha256, bytes, fetched_at, first_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source, source_url, index_url, filename, local_path, refs_json,
             publication_date, sha256, bytes_size, fetched_at, now),
        )
        pdf_id = cur.lastrowid
        inserted = True
    else:
        pdf_id = row["id"]
        # Only fill blanks; don't overwrite a known sha256 / bytes / local_path with NULL.
        conn.execute(
            """
            UPDATE hse_pdfs SET
              source           = ?,
              index_url        = COALESCE(?, index_url),
              filename         = ?,
              local_path       = COALESCE(?, local_path),
              pq_refs_json     = ?,
              publication_date = COALESCE(?, publication_date),
              sha256           = COALESCE(?, sha256),
              bytes            = COALESCE(?, bytes),
              fetched_at       = COALESCE(?, fetched_at)
            WHERE id = ?
            """,
            (source, index_url, filename, local_path, refs_json,
             publication_date, sha256, bytes_size, fetched_at, pdf_id),
        )
        inserted = False
    # Sync junction: replace the full set.
    conn.execute("DELETE FROM hse_pdf_pqs WHERE hse_pdf_id = ?", (pdf_id,))
    if pq_refs:
        conn.executemany(
            "INSERT OR IGNORE INTO hse_pdf_pqs(hse_pdf_id, pq_ref) VALUES (?, ?)",
            [(pdf_id, r) for r in pq_refs],
        )
    return pdf_id, inserted


def hse_pdf_source_urls(conn: sqlite3.Connection) -> set[str]:
    """All source_urls already in hse_pdfs — used by backfill to skip work."""
    return {r[0] for r in conn.execute("SELECT source_url FROM hse_pdfs")}


def hse_pdf_by_id(conn: sqlite3.Connection, pdf_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM hse_pdfs WHERE id = ?", (pdf_id,)).fetchone()


def get_hse_pdfs_for_pq(conn: sqlite3.Connection, pq_ref: str) -> list[dict]:
    """List HSE PDFs whose junction includes this pq_ref."""
    rows = conn.execute(
        """SELECT p.id, p.source, p.source_url, p.index_url, p.filename,
                  p.local_path, p.publication_date, p.bytes
             FROM hse_pdfs p
             JOIN hse_pdf_pqs j ON j.hse_pdf_id = p.id
            WHERE j.pq_ref = ?
            ORDER BY p.source, p.publication_date DESC, p.filename""",
        (pq_ref,),
    ).fetchall()
    return [dict(r) for r in rows]


def replace_hse_paragraphs(conn: sqlite3.Connection, hse_pdf_id: int,
                           paragraphs: list[tuple[int, int, str]]) -> list[int]:
    """Replace the full paragraph set for one PDF. Returns inserted rowids in order.

    `paragraphs` is [(page_no, para_index, text), ...]. The FTS5 mirror is
    content-bound to hse_paragraphs and updated by triggers? No — content-bound
    FTS5 in SQLite is a *contentless-ish* mode: we maintain it ourselves via
    INSERT/DELETE statements that mirror the base table. We do that here.
    """
    # Drop existing paragraphs (cascades to embeddings via source_pdf_id FK on
    # CASCADE — but the FK is on hse_pdfs(id), not hse_paragraphs(id), so we
    # need to manually clear any HSE-paragraph embeddings for this PDF too).
    old_ids = [r[0] for r in conn.execute(
        "SELECT id FROM hse_paragraphs WHERE hse_pdf_id = ?", (hse_pdf_id,)
    )]
    if old_ids:
        conn.executemany(
            "DELETE FROM hse_paragraphs_fts WHERE rowid = ?",
            [(i,) for i in old_ids],
        )
        conn.execute("DELETE FROM hse_paragraphs WHERE hse_pdf_id = ?", (hse_pdf_id,))
    # Also clear semantic embeddings for this PDF — they'll be regenerated.
    conn.execute(
        "DELETE FROM embeddings WHERE source_pdf_id = ? AND source_type = 'hse_paragraph'",
        (hse_pdf_id,),
    )
    new_ids: list[int] = []
    for page_no, para_idx, text in paragraphs:
        cur = conn.execute(
            "INSERT INTO hse_paragraphs(hse_pdf_id, page_no, para_index, text) "
            "VALUES (?, ?, ?, ?)",
            (hse_pdf_id, page_no, para_idx, text),
        )
        rowid = cur.lastrowid
        new_ids.append(rowid)
        conn.execute(
            "INSERT INTO hse_paragraphs_fts(rowid, text) VALUES (?, ?)",
            (rowid, text),
        )
    return new_ids


def set_hse_extraction_status(conn: sqlite3.Connection, hse_pdf_id: int,
                              status: str, page_count: int | None,
                              ocr_used: bool = False) -> None:
    """Record the outcome of an extraction attempt: 'done' | 'empty' | 'failed'."""
    conn.execute(
        """UPDATE hse_pdfs
              SET text_extraction_status = ?,
                  text_extracted_at = ?,
                  text_page_count = ?,
                  ocr_used = ?
            WHERE id = ?""",
        (status, datetime.utcnow().isoformat(timespec="seconds"),
         page_count, 1 if ocr_used else 0, hse_pdf_id),
    )


def get_hse_paragraphs(conn: sqlite3.Connection, hse_pdf_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, page_no, para_index, text FROM hse_paragraphs "
        "WHERE hse_pdf_id = ? ORDER BY page_no, para_index",
        (hse_pdf_id,),
    ).fetchall()


def hse_paragraph_signature(conn: sqlite3.Connection) -> dict[int, int]:
    """For each hse_pdf_id with paragraphs, return paragraph count.

    Mirrors `embeddings.existing_signature()` shape: lets the indexer loop skip
    PDFs whose paragraph count is unchanged.
    """
    rows = conn.execute(
        "SELECT hse_pdf_id, COUNT(*) AS n FROM hse_paragraphs GROUP BY hse_pdf_id"
    ).fetchall()
    return {r["hse_pdf_id"]: r["n"] for r in rows}


def hse_pdf_stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM hse_pdfs").fetchone()[0]
    by_src = dict(conn.execute(
        "SELECT source, COUNT(*) FROM hse_pdfs GROUP BY source"
    ).fetchall())
    matched = conn.execute(
        """SELECT COUNT(DISTINCT j.pq_ref)
             FROM hse_pdf_pqs j
             JOIN questions q ON q.pq_ref = j.pq_ref"""
    ).fetchone()[0]
    by_extract = dict(conn.execute(
        "SELECT COALESCE(text_extraction_status,'unprocessed') AS s, COUNT(*) "
        "  FROM hse_pdfs GROUP BY s"
    ).fetchall())
    n_paragraphs = conn.execute("SELECT COUNT(*) FROM hse_paragraphs").fetchone()[0]
    n_ocr = conn.execute(
        "SELECT COUNT(*) FROM hse_pdfs WHERE ocr_used = 1"
    ).fetchone()[0]
    return {"total": total, "by_source": by_src, "matched_pq_refs": matched,
            "by_extraction": by_extract, "paragraphs": n_paragraphs, "ocr": n_ocr}


def distinct_values(conn: sqlite3.Connection, column: str) -> list[str]:
    if column not in {"td_name", "td_party", "td_constituency", "department", "minister_name"}:
        raise ValueError(f"distinct_values: column {column!r} not whitelisted")
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM questions WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
    ).fetchall()
    return [r[0] for r in rows]
