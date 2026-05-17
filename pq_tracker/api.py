"""Read-only HTTP API for agent consumption.

Registered as a Flask Blueprint on the main UI app. All endpoints under
/api/v1/*. Connections are opened in SQLite read-only mode so endpoints
physically cannot mutate the DB even if a bug tries.

Auth: if env var PQ_API_TOKEN is set, every request must carry
    Authorization: Bearer <token>
If the env var is unset the API runs open — fine for the local-only use
case (Flask is bound to 127.0.0.1), and turns on auth automatically the
moment you tunnel.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from functools import wraps

from flask import Blueprint, jsonify, request

from . import config as cfg


api = Blueprint("api_v1", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Columns the agent ever reads (questions). Excluding xml_raw (BLOB, heavy)
# and raw_question_showas (duplicate of question_text for most rows).
_Q_COLS = (
    "q.pq_ref, q.date_asked, q.date_answered, q.td_name, q.td_party, "
    "q.td_constituency, q.minister_name, q.department, q.answer_status, "
    "q.question_text, q.oireachtas_permalink, q.xml_url, q.pdf_url, "
    "q.hse_pdf_url, q.constituent, q.notes"
)


def _fts_match_expr(s: str) -> str:
    """Build a safe FTS5 MATCH expression from free-form user input.

    AND-joins tokens, supports trailing wildcards (`diab*`), preserves
    double-quoted phrases. All other FTS5 syntax characters are escaped
    by quoting. Returns '' for empty input — callers should short-circuit.
    """
    import shlex
    s = (s or "").strip()
    if not s:
        return ""
    try:
        tokens = shlex.split(s, posix=True)
    except ValueError:
        tokens = s.split()
    parts: list[str] = []
    for t in tokens:
        if not t:
            continue
        if t.endswith("*") and len(t) > 1:
            core = t[:-1].replace('"', '""')
            if core:
                parts.append(f'"{core}"*')
        else:
            parts.append(f'"{t.replace(chr(34), chr(34) * 2)}"')
    return " AND ".join(parts)


def _build_filter_where(args, prefix: str = "q.") -> tuple[str, list]:
    """Parse repeatable filter query-params → (WHERE-fragment, params).

    No leading WHERE — caller composes. Returns ('', []) when no filters set.
    Mirrors the UI's query-string vocabulary so agents and humans stay aligned.
    """
    clauses: list[str] = []
    params: list = []

    multi = {
        "constituency": "td_constituency",
        "member": "td_name",
        "party": "td_party",
        "minister": "minister_name",
        "department": "department",
        "status": "answer_status",
    }
    for arg_name, col in multi.items():
        values = [v.strip() for v in args.getlist(arg_name) if v and v.strip()]
        if values:
            placeholders = ",".join("?" * len(values))
            clauses.append(f"{prefix}{col} IN ({placeholders})")
            params.extend(values)

    tags = [v.strip() for v in args.getlist("tag") if v and v.strip()]
    if tags:
        placeholders = ",".join("?" * len(tags))
        clauses.append(
            f"{prefix}pq_ref IN "
            f"(SELECT pq_ref FROM pq_tags WHERE tag IN ({placeholders}))"
        )
        params.extend(tags)

    for arg, col, op in (
        ("from", "date_asked", ">="),
        ("to", "date_asked", "<="),
        ("from_ans", "date_answered", ">="),
        ("to_ans", "date_answered", "<="),
    ):
        v = (args.get(arg) or "").strip()
        if v:
            clauses.append(f"{prefix}{col} {op} ?")
            params.append(v)

    return " AND ".join(clauses), params


def _snippet(text: str, max_chars: int = 240) -> str:
    """Word-aware truncation for compact list responses."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text.rfind(" ", 0, max_chars)
    if cut < max_chars - 80:
        cut = max_chars
    return text[:cut].rstrip() + "…"


def _pick_snippet(q_marked: str, a_marked: str) -> str:
    """Return whichever FTS snippet actually contained a hit. <mark> markers
    are stripped — agents don't need them, they get the raw highlighted text."""
    for s in (q_marked, a_marked):
        if s and "<mark>" in s:
            return s.replace("<mark>", "").replace("</mark>", "")
    return (q_marked or a_marked or "").replace("<mark>", "").replace("</mark>", "")


def _tags_for(c, refs: list[str]) -> dict[str, list[dict]]:
    if not refs:
        return {}
    placeholders = ",".join("?" * len(refs))
    out: dict[str, list[dict]] = {}
    for r in c.execute(
        f"SELECT pq_ref, tag, state FROM pq_tags WHERE pq_ref IN ({placeholders})",
        refs,
    ):
        out.setdefault(r["pq_ref"], []).append({"tag": r["tag"], "state": r["state"]})
    return out


def _row_to_summary(row, tags_map, snippet_text: str | None = None) -> dict:
    """Compact list-row representation. Full text is only on /pq/<ref>."""
    return {
        "pq_ref": row["pq_ref"],
        "date_asked": row["date_asked"],
        "date_answered": row["date_answered"],
        "member": row["td_name"],
        "party": row["td_party"],
        "constituency": row["td_constituency"],
        "minister": row["minister_name"],
        "department": row["department"],
        "status": row["answer_status"],
        "tags": tags_map.get(row["pq_ref"], []),
        "snippet": snippet_text if snippet_text is not None else _snippet(row["question_text"]),
        "permalink": row["oireachtas_permalink"],
    }


def _require_auth():
    token = (os.environ.get("PQ_API_TOKEN") or "").strip()
    if not token:
        return None
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix) or header[len(prefix):].strip() != token:
        return jsonify({"error": "unauthorized", "code": 401}), 401
    return None


def auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        err = _require_auth()
        if err is not None:
            return err
        return f(*args, **kwargs)
    return wrapper


def _conn() -> sqlite3.Connection:
    """Read-only SQLite connection. Cannot write — enforced by URI mode=ro."""
    uri = f"file:{cfg.DB_PATH}?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


@api.route("/facets", methods=["GET"])
@auth_required
def facets():
    """Discovery endpoint: distinct values for every filter the agent can use.

    Lets the agent ask 'what's queryable?' before guessing constituency names
    or tag spellings. Cheap — sub-10ms on this corpus size.
    """
    c = _conn()
    try:
        def col_distinct(sql):
            return [r[0] for r in c.execute(sql) if r[0]]

        members = [
            {"name": r[0], "party": r[1], "constituency": r[2]}
            for r in c.execute(
                """SELECT DISTINCT td_name, td_party, td_constituency
                     FROM questions
                    WHERE td_name IS NOT NULL AND td_name != ''
                    ORDER BY td_name"""
            )
        ]
        date_min, date_max = c.execute(
            "SELECT MIN(date_asked), MAX(date_asked) FROM questions"
        ).fetchone()
        total = c.execute("SELECT COUNT(*) FROM questions").fetchone()[0]

        return jsonify({
            "constituencies": col_distinct(
                "SELECT DISTINCT td_constituency FROM questions "
                "WHERE td_constituency IS NOT NULL ORDER BY td_constituency"
            ),
            "parties": col_distinct(
                "SELECT DISTINCT td_party FROM questions "
                "WHERE td_party IS NOT NULL ORDER BY td_party"
            ),
            "ministers": col_distinct(
                "SELECT DISTINCT minister_name FROM questions "
                "WHERE minister_name IS NOT NULL ORDER BY minister_name"
            ),
            "departments": col_distinct(
                "SELECT DISTINCT department FROM questions ORDER BY department"
            ),
            "members": members,
            "tags": {
                "auto": col_distinct(
                    "SELECT DISTINCT tag FROM pq_tags WHERE state='auto' ORDER BY tag"
                ),
                "user": col_distinct(
                    "SELECT DISTINCT tag FROM pq_tags WHERE state='user_added' ORDER BY tag"
                ),
            },
            "statuses": ["pending", "answered"],
            "date_range": {"min": date_min, "max": date_max},
            "total_pqs": total,
        })
    finally:
        c.close()


@api.route("/pqs", methods=["GET"])
@auth_required
def list_pqs():
    """List/filter/search PQs with pagination.

    Filters (all optional, all repeatable except dates and q):
      constituency, member, party, minister, department, status, tag
      from / to             — date_asked range  (YYYY-MM-DD)
      from_ans / to_ans     — date_answered range
      q                     — FTS5 search; when present, drives ordering
      limit (default 50, max 500), offset (default 0)

    Returns compact rows + snippet. Full text on /pq/<ref>.
    """
    args = request.args
    q = (args.get("q") or "").strip()
    try:
        limit = int(args.get("limit") or 50)
        offset = int(args.get("offset") or 0)
    except ValueError:
        return jsonify({"error": "limit/offset must be integers", "code": 400}), 400
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    filter_where, filter_params = _build_filter_where(args)

    c = _conn()
    try:
        if q:
            expr = _fts_match_expr(q)
            if not expr:
                return jsonify({"total": 0, "offset": offset, "limit": limit, "items": []})
            # FTS5 join: filter + rank in one go. snippet() with <mark>
            # markers so we can detect which side hit.
            select = (
                f"{_Q_COLS}, "
                "bm25(questions_fts) AS score, "
                "snippet(questions_fts, 0, '<mark>', '</mark>', '…', 20) AS snip_q, "
                "snippet(questions_fts, 1, '<mark>', '</mark>', '…', 20) AS snip_a"
            )
            base = (
                "FROM questions_fts "
                "JOIN questions q ON q.pq_ref = questions_fts.pq_ref "
                "WHERE questions_fts MATCH ?"
            )
            params: list = [expr]
            if filter_where:
                base += " AND " + filter_where
                params.extend(filter_params)
            total = c.execute(
                f"SELECT COUNT(*) {base}", params
            ).fetchone()[0]
            page_rows = c.execute(
                f"SELECT {select} {base} ORDER BY score LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        else:
            base = "FROM questions q"
            params = []
            if filter_where:
                base += " WHERE " + filter_where
                params.extend(filter_params)
            total = c.execute(
                f"SELECT COUNT(*) {base}", params
            ).fetchone()[0]
            page_rows = c.execute(
                f"SELECT {_Q_COLS} {base} "
                "ORDER BY q.date_asked DESC, q.pq_ref DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        tags_map = _tags_for(c, [r["pq_ref"] for r in page_rows])
        items: list[dict] = []
        for r in page_rows:
            if q:
                snip = _pick_snippet(r["snip_q"], r["snip_a"])
            else:
                snip = _snippet(r["question_text"])
            items.append(_row_to_summary(r, tags_map, snippet_text=snip))

        return jsonify({
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        })
    finally:
        c.close()


@api.route("/pq/<path:pq_ref>", methods=["GET"])
@auth_required
def get_pq(pq_ref):
    """Single PQ. ?include=question,answer,tags,hse_pdfs,xml (defaults to all
    but xml — XML rendering is opt-in because it's the heaviest field).

    `xml` returns akoma-ntoso-rendered HTML for both question and answer; use
    `answer` instead if you just want plain text.
    """
    raw = (request.args.get("include") or "").strip()
    if raw:
        include = {t.strip() for t in raw.split(",") if t.strip()}
    else:
        include = {"question", "answer", "tags", "hse_pdfs"}

    c = _conn()
    try:
        row = c.execute(
            f"SELECT {_Q_COLS}, q.question_uri "
            "FROM questions q WHERE q.pq_ref = ?",
            (pq_ref,),
        ).fetchone()
        if not row:
            return jsonify({"error": "not found", "code": 404, "pq_ref": pq_ref}), 404

        out: dict = {
            "pq_ref": row["pq_ref"],
            "date_asked": row["date_asked"],
            "date_answered": row["date_answered"],
            "member": row["td_name"],
            "party": row["td_party"],
            "constituency": row["td_constituency"],
            "minister": row["minister_name"],
            "department": row["department"],
            "status": row["answer_status"],
            "permalink": row["oireachtas_permalink"],
            "xml_url": row["xml_url"],
            "pdf_url": row["pdf_url"],
            "hse_pdf_url": row["hse_pdf_url"],
            "constituent": row["constituent"],
            "notes": row["notes"],
        }

        if "question" in include:
            out["question_text"] = row["question_text"]

        if "answer" in include:
            ans = c.execute(
                "SELECT a.answer_text FROM answers a "
                "JOIN questions q ON a.id = q.answer_id "
                "WHERE q.pq_ref = ?",
                (pq_ref,),
            ).fetchone()
            out["answer_text"] = ans["answer_text"] if ans else None

        if "tags" in include:
            out["tags"] = _tags_for(c, [pq_ref]).get(pq_ref, [])

        if "hse_pdfs" in include:
            pdfs = c.execute(
                """SELECT h.id, h.source, h.source_url, h.index_url, h.filename,
                          h.publication_date, h.text_extraction_status,
                          h.text_page_count
                     FROM hse_pdfs h
                     JOIN hse_pdf_pqs j ON j.hse_pdf_id = h.id
                    WHERE j.pq_ref = ?
                    ORDER BY h.publication_date DESC""",
                (pq_ref,),
            ).fetchall()
            out["hse_pdfs"] = [
                {
                    "id": p["id"],
                    "source": p["source"],
                    "source_url": p["source_url"],
                    "index_url": p["index_url"],
                    "filename": p["filename"],
                    "publication_date": p["publication_date"],
                    "text_status": p["text_extraction_status"],
                    "pages": p["text_page_count"],
                }
                for p in pdfs
            ]

        if "xml" in include:
            from . import akoma_html
            xml_row = c.execute(
                "SELECT xml_raw, question_uri FROM questions WHERE pq_ref = ?",
                (pq_ref,),
            ).fetchone()
            q_html = a_html = None
            if xml_row and xml_row["xml_raw"]:
                uri = xml_row["question_uri"] or ""
                e_id = uri.rsplit("/", 1)[-1] if uri else ""
                if e_id:
                    q_html, a_html = akoma_html.render_question_and_answer(
                        xml_row["xml_raw"], e_id
                    )
            out["question_html"] = q_html
            out["answer_html"] = a_html

        return jsonify(out)
    finally:
        c.close()


_AGG_GROUPS = {
    # group_by name → (SQL expression, sortable key for ties)
    "constituency": "q.td_constituency",
    "member": "q.td_name",
    "party": "q.td_party",
    "minister": "q.minister_name",
    "department": "q.department",
    "status": "q.answer_status",
    "month": "strftime('%Y-%m', q.date_asked)",
    "year": "strftime('%Y', q.date_asked)",
}


@api.route("/aggregate", methods=["GET"])
@auth_required
def aggregate():
    """Count PQs grouped by a single dimension, optionally filtered.

    group_by ∈ {constituency, member, party, minister, department, status,
                month, year, tag}

    Accepts the same filters as /pqs. Returns `buckets` sorted by count DESC.

    For group_by=tag, sum(counts) > total_pqs is normal — a PQ can carry
    multiple tags. For every other dimension each PQ contributes to exactly
    one bucket.
    """
    args = request.args
    group_by = (args.get("group_by") or "").strip()
    allowed = list(_AGG_GROUPS.keys()) + ["tag"]
    if not group_by:
        return jsonify({"error": "group_by required", "allowed": allowed, "code": 400}), 400
    if group_by not in allowed:
        return jsonify({"error": f"invalid group_by: {group_by}", "allowed": allowed, "code": 400}), 400

    try:
        limit = int(args.get("limit") or 200)
    except ValueError:
        return jsonify({"error": "limit must be integer", "code": 400}), 400
    limit = max(1, min(limit, 2000))

    filter_where, filter_params = _build_filter_where(args)
    q = (args.get("q") or "").strip()

    # Inner subquery that yields the pq_refs matching every active filter.
    # All aggregates run against this set, keeping the filter logic in one
    # place regardless of group_by dimension.
    if q:
        expr = _fts_match_expr(q)
        if not expr:
            return jsonify({"group_by": group_by, "total_pqs": 0, "buckets": []})
        inner_sql = (
            "SELECT q.pq_ref FROM questions_fts "
            "JOIN questions q ON q.pq_ref = questions_fts.pq_ref "
            "WHERE questions_fts MATCH ?"
        )
        inner_params: list = [expr]
        if filter_where:
            inner_sql += " AND " + filter_where
            inner_params.extend(filter_params)
    else:
        inner_sql = "SELECT q.pq_ref FROM questions q"
        inner_params = []
        if filter_where:
            inner_sql += " WHERE " + filter_where
            inner_params.extend(filter_params)

    c = _conn()
    try:
        total_pqs = c.execute(
            f"SELECT COUNT(*) FROM ({inner_sql})", inner_params
        ).fetchone()[0]

        if group_by == "tag":
            rows = c.execute(
                f"""SELECT pt.tag AS key, COUNT(DISTINCT pt.pq_ref) AS count
                      FROM pq_tags pt
                     WHERE pt.pq_ref IN ({inner_sql})
                     GROUP BY pt.tag
                     ORDER BY count DESC, pt.tag
                     LIMIT ?""",
                inner_params + [limit],
            ).fetchall()
        else:
            col_expr = _AGG_GROUPS[group_by]
            rows = c.execute(
                f"""SELECT {col_expr} AS key, COUNT(*) AS count
                      FROM questions q
                     WHERE q.pq_ref IN ({inner_sql})
                       AND {col_expr} IS NOT NULL
                     GROUP BY key
                     ORDER BY count DESC, key
                     LIMIT ?""",
                inner_params + [limit],
            ).fetchall()

        return jsonify({
            "group_by": group_by,
            "total_pqs": total_pqs,
            "buckets": [{"key": r["key"], "count": r["count"]} for r in rows],
        })
    finally:
        c.close()


@api.route("/semantic", methods=["GET"])
@auth_required
def semantic():
    """Cosine top-k over BGE-small embeddings.

    Params:
      q       — natural-language query (required)
      source  — question | answer | hse_paragraph (default question)
      limit   — 1..200, default 20
      Plus standard filters (constituency, member, party, minister, department,
      tag, status, from/to, from_ans/to_ans) — applied as a post-filter on
      pq_ref. Ignored for source=hse_paragraph.

    Use this when lexical FTS misses (paraphrases, synonyms, the agent
    chasing a vague concept). For exact terms /pqs?q=... is usually better.
    """
    args = request.args
    q = (args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q required", "code": 400}), 400

    source = (args.get("source") or "question").strip()
    allowed_sources = {"question", "answer", "hse_paragraph"}
    if source not in allowed_sources:
        return jsonify({
            "error": f"invalid source: {source}",
            "allowed": sorted(allowed_sources),
            "code": 400,
        }), 400

    try:
        limit = int(args.get("limit") or 20)
    except ValueError:
        return jsonify({"error": "limit must be integer", "code": 400}), 400
    limit = max(1, min(limit, 200))

    from . import embeddings as emb_mod

    c = _conn()
    try:
        try:
            qvec = emb_mod.embed_texts([q])[0]
        except Exception as e:
            return jsonify({"error": f"embedding failed: {e}", "code": 500}), 500

        meta, matrix = emb_mod.load_matrix(c, source_type=source)
        if matrix.shape[0] == 0:
            return jsonify({"source": source, "query": q, "items": []})

        # Optional post-filter on pq_ref set (only meaningful for question/answer).
        allowed_refs: set | None = None
        if source in ("question", "answer"):
            filter_where, filter_params = _build_filter_where(args)
            if filter_where:
                allowed_refs = {
                    r[0] for r in c.execute(
                        f"SELECT q.pq_ref FROM questions q WHERE {filter_where}",
                        filter_params,
                    )
                }
                if not allowed_refs:
                    return jsonify({"source": source, "query": q, "items": []})

        # Pull a wider candidate set than `limit` because we'll dedup by pq_ref
        # (one PQ contributes several chunks) and possibly drop some via the
        # post-filter.
        k = min(matrix.shape[0], max(limit * 4, 50))
        order, scores = emb_mod.cosine_topk(qvec, matrix, k=k)

        items: list[dict] = []
        seen: set = set()
        for idx, score in zip(order, scores):
            row_id, pq_ref, pdf_id, chunk_idx, text = meta[idx]
            if source == "hse_paragraph":
                key = (pdf_id, chunk_idx)
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "hse_pdf_id": pdf_id,
                    "chunk_index": chunk_idx,
                    "score": float(score),
                    "excerpt": (text or "")[:240],
                })
            else:
                if not pq_ref or pq_ref in seen:
                    continue
                if allowed_refs is not None and pq_ref not in allowed_refs:
                    continue
                seen.add(pq_ref)
                items.append({
                    "pq_ref": pq_ref,
                    "chunk_index": chunk_idx,
                    "score": float(score),
                    "excerpt": (text or "")[:240],
                })
            if len(items) >= limit:
                break

        return jsonify({"source": source, "query": q, "items": items})
    finally:
        c.close()


@api.route("/hse_pdfs", methods=["GET"])
@auth_required
def list_hse_pdfs():
    """List HSE supplementary PDFs with filter + pagination.

    Filters:
      source         — multi (e.g. hse_live, wayback)
      text_status    — multi (e.g. ok, corrupt, missing)
      from / to      — publication_date range (YYYY-MM-DD)
      pq_ref         — only PDFs linked to this PQ
      limit / offset — default 50 / 0

    Returns compact metadata + linked pq_refs per PDF.
    Full paragraph text is on /hse_pdf/<id>.
    """
    args = request.args
    try:
        limit = int(args.get("limit") or 50)
        offset = int(args.get("offset") or 0)
    except ValueError:
        return jsonify({"error": "limit/offset must be integers", "code": 400}), 400
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    clauses: list[str] = []
    params: list = []
    for arg_name, col in (("source", "source"), ("text_status", "text_extraction_status")):
        values = [v.strip() for v in args.getlist(arg_name) if v and v.strip()]
        if values:
            placeholders = ",".join("?" * len(values))
            clauses.append(f"{col} IN ({placeholders})")
            params.extend(values)
    for arg, op in (("from", ">="), ("to", "<=")):
        v = (args.get(arg) or "").strip()
        if v:
            clauses.append(f"publication_date {op} ?")
            params.append(v)
    pq_ref = (args.get("pq_ref") or "").strip()
    if pq_ref:
        clauses.append("id IN (SELECT hse_pdf_id FROM hse_pdf_pqs WHERE pq_ref = ?)")
        params.append(pq_ref)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    c = _conn()
    try:
        total = c.execute(f"SELECT COUNT(*) FROM hse_pdfs{where}", params).fetchone()[0]
        # SQLite has no NULLS LAST — emulate with the IS NULL trick.
        rows = c.execute(
            f"""SELECT id, source, source_url, index_url, filename,
                       publication_date, sha256, bytes,
                       text_extraction_status, text_extracted_at,
                       text_page_count, ocr_used, fetched_at, first_seen_at,
                       pq_refs_json
                  FROM hse_pdfs{where}
                 ORDER BY publication_date IS NULL, publication_date DESC, id DESC
                 LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        items = []
        for r in rows:
            try:
                pq_refs = json.loads(r["pq_refs_json"] or "[]")
            except json.JSONDecodeError:
                pq_refs = []
            items.append({
                "id": r["id"],
                "source": r["source"],
                "source_url": r["source_url"],
                "index_url": r["index_url"],
                "filename": r["filename"],
                "publication_date": r["publication_date"],
                "sha256": r["sha256"],
                "bytes": r["bytes"],
                "text_status": r["text_extraction_status"],
                "text_extracted_at": r["text_extracted_at"],
                "pages": r["text_page_count"],
                "ocr_used": bool(r["ocr_used"]) if r["ocr_used"] is not None else None,
                "fetched_at": r["fetched_at"],
                "first_seen_at": r["first_seen_at"],
                "pq_refs": pq_refs,
            })
        return jsonify({"total": total, "offset": offset, "limit": limit, "items": items})
    finally:
        c.close()


@api.route("/hse_pdf/<int:pdf_id>", methods=["GET"])
@auth_required
def get_hse_pdf(pdf_id):
    """Single HSE PDF. ?include=paragraphs,pqs,text (default paragraphs + pqs).

    `paragraphs` returns structured rows (page, para_index, text) — useful for
    citing a specific page. `text` returns the same content concatenated into
    one block — useful when you just want to feed it to the agent.
    """
    raw = (request.args.get("include") or "").strip()
    if raw:
        include = {t.strip() for t in raw.split(",") if t.strip()}
    else:
        include = {"paragraphs", "pqs"}

    c = _conn()
    try:
        row = c.execute(
            """SELECT id, source, source_url, index_url, filename, local_path,
                      publication_date, sha256, bytes, fetched_at, first_seen_at,
                      text_extraction_status, text_extracted_at, text_page_count,
                      ocr_used, pq_refs_json
                 FROM hse_pdfs WHERE id = ?""",
            (pdf_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": "not found", "code": 404, "id": pdf_id}), 404

        try:
            pq_refs_json = json.loads(row["pq_refs_json"] or "[]")
        except json.JSONDecodeError:
            pq_refs_json = []

        out = {
            "id": row["id"],
            "source": row["source"],
            "source_url": row["source_url"],
            "index_url": row["index_url"],
            "filename": row["filename"],
            "local_path": row["local_path"],
            "publication_date": row["publication_date"],
            "sha256": row["sha256"],
            "bytes": row["bytes"],
            "fetched_at": row["fetched_at"],
            "first_seen_at": row["first_seen_at"],
            "text_status": row["text_extraction_status"],
            "text_extracted_at": row["text_extracted_at"],
            "pages": row["text_page_count"],
            "ocr_used": bool(row["ocr_used"]) if row["ocr_used"] is not None else None,
            "pq_refs_meta": pq_refs_json,
        }

        if "pqs" in include:
            out["pq_refs"] = [
                r[0] for r in c.execute(
                    "SELECT pq_ref FROM hse_pdf_pqs WHERE hse_pdf_id = ? ORDER BY pq_ref",
                    (pdf_id,),
                )
            ]

        if "paragraphs" in include or "text" in include:
            paras = c.execute(
                "SELECT id, page_no, para_index, text FROM hse_paragraphs "
                "WHERE hse_pdf_id = ? ORDER BY page_no, para_index",
                (pdf_id,),
            ).fetchall()
            if "paragraphs" in include:
                out["paragraphs"] = [
                    {"id": p["id"], "page": p["page_no"], "index": p["para_index"], "text": p["text"]}
                    for p in paras
                ]
            if "text" in include:
                out["text"] = "\n\n".join(p["text"] for p in paras if p["text"])

        return jsonify(out)
    finally:
        c.close()


_SQL_MAX_ROWS = 1000
_SQL_TIMEOUT_S = 8


def _jsonable(v):
    """Coerce a SQLite column value to something JSON can serialize."""
    if isinstance(v, (bytes, memoryview)):
        # Base64 BLOBs (xml_raw, embeddings.vector) — agent can decode if it
        # really wants them, but they're rarely useful inline.
        return {"_blob_b64": base64.b64encode(bytes(v)).decode("ascii"),
                "_blob_len": len(bytes(v))}
    return v


@api.route("/sql", methods=["POST"])
@auth_required
def run_sql():
    """Read-only SQL escape hatch.

    Body: JSON {"sql": "SELECT ..."} or {"sql": "...", "params": [...]}.

    Returns: {columns, rows, row_count, truncated}.

    Constraints:
    - The connection is opened with mode=ro — DML/DDL is rejected by SQLite.
    - Single statement only — sqlite3.execute() naturally rejects stacked statements.
    - Auto-injects LIMIT 1000 when the query has no LIMIT clause.
    - Statement aborts after ~8s of CPU work (progress handler).

    Use when the curated endpoints don't expose what you need — ad-hoc
    aggregates, joins across tables, schema introspection
    (e.g. SELECT * FROM sqlite_master).
    """
    data = request.get_json(silent=True) or {}
    sql = (data.get("sql") or "").strip()
    params = data.get("params") or []
    if not sql:
        return jsonify({"error": "sql required", "code": 400}), 400
    if not isinstance(params, list):
        return jsonify({"error": "params must be a list", "code": 400}), 400

    # Cheap LIMIT injection — case-insensitive, only when no LIMIT clause is
    # present at all. Subqueries with their own LIMIT still get an outer cap.
    sql_stripped = sql.rstrip().rstrip(";").rstrip()
    if " limit " not in (" " + sql_stripped.lower() + " "):
        sql_stripped = f"{sql_stripped} LIMIT {_SQL_MAX_ROWS}"

    c = _conn()
    deadline_hit = {"flag": False}

    import time
    start = time.monotonic()

    def cancel_check():
        # Progress handler — return non-zero to abort the query.
        if time.monotonic() - start > _SQL_TIMEOUT_S:
            deadline_hit["flag"] = True
            return 1
        return 0

    # Fire every 1000 VM instructions — granular enough that a runaway query
    # is caught within milliseconds of crossing the deadline.
    c.set_progress_handler(cancel_check, 1000)

    try:
        cur = c.execute(sql_stripped, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(_SQL_MAX_ROWS + 1)
        truncated = len(rows) > _SQL_MAX_ROWS
        rows = rows[:_SQL_MAX_ROWS]
        out_rows = [[_jsonable(v) for v in r] for r in rows]
        return jsonify({
            "columns": cols,
            "rows": out_rows,
            "row_count": len(out_rows),
            "truncated": truncated,
        })
    except sqlite3.Error as e:
        msg = str(e)
        if deadline_hit["flag"]:
            return jsonify({"error": f"query exceeded {_SQL_TIMEOUT_S}s timeout",
                            "code": 408}), 408
        return jsonify({"error": msg, "code": 400}), 400
    finally:
        c.set_progress_handler(None, 0)
        c.close()
