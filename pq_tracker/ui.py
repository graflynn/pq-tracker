"""Local Flask UI to browse and lightly edit the pq-tracker corpus.

Run with:
    python -m pq_tracker.ui

Defaults to http://127.0.0.1:5454. Editable fields: constituent, hse_pdf_url.
All other data is read-only (regenerated each ingest run).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import webbrowser
from datetime import date

import io
from datetime import date as _date

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from pathlib import Path

from . import config as cfg
from . import db
from . import exports

PAGE_SIZE = 50

app = Flask(__name__, template_folder="templates")


_PARTY_ABBREV = {
    "Fianna Fáil": "FF",
    "Fine Gael": "FG",
    "Sinn Féin": "SF",
    "Labour": "Lab",
    "Labour Party": "Lab",
    "Green Party": "GP",
    "Social Democrats": "SD",
    "People Before Profit-Solidarity": "PBP",
    "People Before Profit": "PBP",
    "Solidarity": "Sol",
    "Aontú": "AON",
    "Independent": "Ind",
    "Independents 4 Change": "I4C",
    "Rural Independent Group": "RIG",
    "Renua": "Ren",
    "Workers' Party": "WP",
    "Progressive Democrats": "PD",
}


def _party_abbrev(name: str | None) -> str:
    """Short label for the in-cell party chip. Falls back to first-letter
    initials for unknown parties so new ones still render something readable."""
    if not name:
        return ""
    if name in _PARTY_ABBREV:
        return _PARTY_ABBREV[name]
    words = [w for w in name.split() if w and w[0].isalpha()]
    abbrev = "".join(w[0].upper() for w in words[:4])
    return abbrev or name[:3].upper()


app.jinja_env.globals["party_abbrev"] = _party_abbrev


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(cfg.DB_PATH, timeout=30.0)
    c.row_factory = sqlite3.Row
    # Wait up to 30s if a long-running writer (backfill / ingest) holds the lock.
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _all_topics(conn: sqlite3.Connection) -> list[str]:
    seen: set[str] = set()
    for r in conn.execute("SELECT matched_topics FROM questions"):
        try:
            for t in json.loads(r[0] or "[]"):
                seen.add(t)
        except json.JSONDecodeError:
            pass
    return sorted(seen)


_SORT_COLS = {
    # Map ?sort= value → SQL column. Whitelist (no user input goes into SQL).
    "pq_ref": "pq_ref",
    "date_asked": "date_asked",
    "td_name": "td_name",
    "td_party": "td_party",
    "td_constituency": "td_constituency",
    "department": "department",
    "answer_status": "answer_status",
    "date_answered": "date_answered",
    "constituent": "constituent",
    "notes": "notes",
}


def _multi(args, key: str) -> list[str]:
    """Repeated query-string values, blank-stripped, dedup-preserving-order."""
    seen: set[str] = set()
    out: list[str] = []
    for v in args.getlist(key):
        v = (v or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _parse_filters(args) -> dict:
    """Search bar (?q= + ?d= + ?i=) + column-header filters (multi-select).

    Column-header filters arrive as repeated query-string keys:
      ?td=A&td=B  → ["A", "B"]   (OR-matched in SQL with IN)
    Date asked from/to and the two quick toggles round it out.

    Sort: ?sort=<col>&dir=asc|desc. Both optional; falls back to relevance
    when a search is active, otherwise date_asked DESC.
    """
    domains = set(args.getlist("d")) or {"question", "answer"}
    indexes = set(args.getlist("i")) or {"lex", "sem"}
    sort_raw = (args.get("sort") or "").strip()
    sort = sort_raw if sort_raw in _SORT_COLS else ""
    dir_raw = (args.get("dir") or "").strip().lower()
    direction = dir_raw if dir_raw in ("asc", "desc") else ("desc" if sort else "")
    # Grouping toggle. Default on — see project memory, the user wants grouped
    # rows by default. `?group=0` flattens.
    group_raw = (args.get("group") or "").strip()
    group_on = group_raw != "0"
    return {
        "q": (args.get("q") or "").strip(),
        "domains": domains,
        "indexes": indexes,
        "group": group_on,
        # Multi-select column filters.
        "td": _multi(args, "td"),
        "party": _multi(args, "party"),
        "constituency": _multi(args, "constituency"),
        "dept": _multi(args, "dept"),
        "topic": _multi(args, "topic"),
        "tag": _multi(args, "tag"),
        "status": _multi(args, "status"),
        # Date asked range.
        "from": (args.get("from") or "").strip(),
        "to": (args.get("to") or "").strip(),
        # Date answered range.
        "from_ans": (args.get("from_ans") or "").strip(),
        "to_ans": (args.get("to_ans") or "").strip(),
        # Text-substring filters.
        "pqref": (args.get("pqref") or "").strip(),
        "constit": (args.get("constit") or "").strip(),
        "notes": (args.get("notes") or "").strip(),
        # Sort.
        "sort": sort,
        "dir": direction,
    }


def _search_prefilter(conn, f: dict, limit: int = 500) -> tuple[list[str] | None, dict[str, float]]:
    """Run the unified search (?q=...) across the selected domains × indexes.

    domains ⊆ {question, answer, hse} — which body of text to search
    indexes ⊆ {lex, sem}             — which index/algorithm to use

    Each (domain, index) combination yields one ranked list of pq_refs. The
    lists are merged with Reciprocal Rank Fusion (k=60) into a single score
    map. Returns (ordered_pq_refs, score_map), or (None, {}) when no query
    is present so the caller falls back to default date ordering.
    """
    q = (f.get("q") or "").strip()
    domains = f.get("domains") or {"question", "answer"}
    indexes = f.get("indexes") or {"lex", "sem"}
    if not q:
        return None, {}

    text_domains = [d for d in ("question", "answer") if d in domains]
    want_hse = "hse" in domains
    if not text_domains and not want_hse:
        return [], {}

    ranked_lists: list[list[dict]] = []

    # Lexical (BM25, porter-stemmed FTS5).
    if "lex" in indexes:
        expr = _fts_escape(q)
        if expr:
            for domain in text_domains:
                col = f"{domain}_text"
                match_expr = f"{{{col}}}: {expr}"
                try:
                    rows = _query_fts(conn, "questions_fts", match_expr, limit)
                except sqlite3.OperationalError as e:
                    log_app(f"FTS failed for domain={domain}: {e}")
                    continue
                ranked_lists.append([{"pq_ref": r["pq_ref"]} for r in rows])
            if want_hse:
                try:
                    pq_refs = _query_hse_fts(conn, expr, limit)
                except sqlite3.OperationalError as e:
                    log_app(f"HSE FTS failed: {e}")
                    pq_refs = []
                if pq_refs:
                    ranked_lists.append([{"pq_ref": r} for r in pq_refs])

    # Semantic (BGE embeddings, cosine).
    if "sem" in indexes:
        from . import embeddings as emb_mod
        try:
            qvec = emb_mod.embed_texts([q])[0]
        except Exception as e:
            log_app(f"semantic embed failed: {e}")
            qvec = None
        if qvec is not None:
            for domain in text_domains:
                meta, matrix = _emb_load(conn, domain)
                if matrix.shape[0] == 0:
                    continue
                order, scores = emb_mod.cosine_topk(qvec, matrix, k=limit)
                seen: dict[str, bool] = {}
                for idx in order:
                    _, pq_ref, _, _, _ = meta[idx]
                    if pq_ref and pq_ref not in seen:
                        seen[pq_ref] = True
                ranked_lists.append([{"pq_ref": r} for r in seen.keys()])
            if want_hse:
                pq_refs = _semantic_hse_pq_refs(conn, qvec, limit)
                if pq_refs:
                    ranked_lists.append([{"pq_ref": r} for r in pq_refs])

    if not ranked_lists:
        return [], {}

    score_map = _rrf_merge(*ranked_lists)
    ranked = sorted(score_map, key=lambda r: -score_map[r])
    return ranked, score_map


def _pdf_pqrefs_map(conn, pdf_ids: list[int]) -> dict[int, list[str]]:
    """Bulk-fetch pq_refs joined to each hse_pdf_id."""
    if not pdf_ids:
        return {}
    placeholders = ",".join("?" * len(pdf_ids))
    out: dict[int, list[str]] = {}
    for r in conn.execute(
        f"SELECT hse_pdf_id, pq_ref FROM hse_pdf_pqs WHERE hse_pdf_id IN ({placeholders})",
        pdf_ids,
    ):
        out.setdefault(r[0], []).append(r[1])
    return out


def _query_hse_fts(conn, escaped_expr: str, limit: int) -> list[str]:
    """BM25 over hse_paragraphs_fts → ranked pq_refs.

    A paragraph hit can map to several PQs (grouped questions on one PDF);
    each PQ surfaces at the best score it gets. Returns pq_refs ordered by
    that best-rank, capped at `limit` overall.
    """
    rows = conn.execute(
        """SELECT hp.hse_pdf_id, bm25(hse_paragraphs_fts) AS score
             FROM hse_paragraphs_fts
             JOIN hse_paragraphs hp ON hp.id = hse_paragraphs_fts.rowid
            WHERE hse_paragraphs_fts MATCH ?
            ORDER BY score
            LIMIT ?""",
        (escaped_expr, limit),
    ).fetchall()
    if not rows:
        return []
    pdf_ids_in_order: list[int] = []
    seen_pdf: set[int] = set()
    for r in rows:
        pid = r["hse_pdf_id"]
        if pid not in seen_pdf:
            seen_pdf.add(pid)
            pdf_ids_in_order.append(pid)
    pdf_map = _pdf_pqrefs_map(conn, pdf_ids_in_order)
    out: list[str] = []
    seen_pq: set[str] = set()
    for pid in pdf_ids_in_order:
        for ref in pdf_map.get(pid, []):
            if ref not in seen_pq:
                seen_pq.add(ref)
                out.append(ref)
                if len(out) >= limit:
                    return out
    return out


def _semantic_hse_pq_refs(conn, qvec, limit: int) -> list[str]:
    """Cosine top-k over HSE paragraph embeddings → ranked pq_refs."""
    from . import embeddings as emb_mod
    meta, matrix = _emb_load(conn, "hse_paragraph")
    if matrix.shape[0] == 0:
        return []
    # Pull more candidates than `limit` because many will share a PDF and
    # collapse together — and because one PDF can fan back out to several PQs.
    order, _ = emb_mod.cosine_topk(qvec, matrix, k=min(matrix.shape[0], limit * 4))
    pdf_ids_in_order: list[int] = []
    seen_pdf: set[int] = set()
    for idx in order:
        _, _, source_pdf_id, _, _ = meta[idx]
        if source_pdf_id and source_pdf_id not in seen_pdf:
            seen_pdf.add(source_pdf_id)
            pdf_ids_in_order.append(source_pdf_id)
    pdf_map = _pdf_pqrefs_map(conn, pdf_ids_in_order)
    out: list[str] = []
    seen_pq: set[str] = set()
    for pid in pdf_ids_in_order:
        for ref in pdf_map.get(pid, []):
            if ref not in seen_pq:
                seen_pq.add(ref)
                out.append(ref)
                if len(out) >= limit:
                    return out
    return out


def log_app(msg: str) -> None:
    """Tiny stub so search prefilter can warn into the Flask log."""
    try:
        app.logger.warning(msg)
    except Exception:
        pass


# Boilerplate prefix that every PQ question text starts with:
#   "{agenda_num}. Deputy {Name}[, Deputy {Name}, ...] asked the {Minister Title}"
# followed by the actual question. The minister title and TD identity are
# already on the modal's summary strip, so the prefix is pure noise. Strip it
# for display only — DB/FTS/exports keep the original.
import re as _re_strip

# HTML form rendered by akoma_html: <p>NUM. <strong>Deputy ...</strong> asked the <strong>TITLE</strong> ...
# Accepts <b> too in case the renderer emits that.
_BOILER_HTML = _re_strip.compile(
    r'^(\s*<p[^>]*>)\s*\d+\.\s+'
    r'<(?:strong|b)>\s*Deputy[^<]+</(?:strong|b)>\s+'
    r'asked\s+the\s+'
    r'<(?:strong|b)>\s*[^<]+</(?:strong|b)>\s*'
)
# Plain-text form. The minister title is a run of TitleCased words optionally
# linked by short connectors (for/of/at/the/and/in) — e.g. "Minister of State
# at the Department of Health". Stops at the first non-title word, which is
# the start of the actual question ("if", "the reason", "to provide", etc).
_BOILER_TEXT = _re_strip.compile(
    r'^\s*\d+\.\s+Deputy\s+'
    r'(?:[A-ZÀ-Ý][\w.\'\-À-ſ]*[,]?\s+)+'
    r'asked\s+the\s+'
    r'(?:'
        r'[A-ZÀ-Ý][\w.\'\-À-ſ]*[,]?\s+'
        r'|(?:(?:for|of|at|the|and|in)\s+)+(?=[A-ZÀ-Ý])'
    r')+'
)


def _strip_question_boilerplate(text):
    """Strip the agenda-number + 'Deputy X asked the Minister Y' prefix.
    Returns input unchanged if no match. UI-only — do not apply to exports or
    to text indexed for search."""
    if not text:
        return text
    m = _BOILER_HTML.match(text)
    if m:
        return m.group(1) + '… ' + text[m.end():].lstrip()
    m = _BOILER_TEXT.match(text)
    if m:
        return '… ' + text[m.end():].lstrip()
    return text


def _in_clause(col: str, values: list[str]) -> tuple[str, list]:
    """Build `col IN (?,?,...)` for a non-empty list. Returns ('', []) if empty."""
    if not values:
        return "", []
    placeholders = ",".join("?" * len(values))
    return f"{col} IN ({placeholders})", list(values)


def _build_where(f: dict) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    for col, key in (
        ("td_name", "td"),
        ("td_party", "party"),
        ("td_constituency", "constituency"),
        ("department", "dept"),
    ):
        clause, p = _in_clause(col, f[key])
        if clause:
            clauses.append(clause)
            params.extend(p)
    if f["topic"]:
        # matched_topics is a JSON array stored as text; OR substring-match each.
        parts = []
        for t in f["topic"]:
            parts.append("matched_topics LIKE ?")
            params.append(f"%\"{t}\"%")
        clauses.append("(" + " OR ".join(parts) + ")")
    if f["tag"]:
        placeholders = ",".join("?" * len(f["tag"]))
        clauses.append(f"pq_ref IN (SELECT pq_ref FROM tags WHERE tag IN ({placeholders}))")
        params.extend(f["tag"])
    if f["status"]:
        # answer_status is one of {'pending','answered'}; reuse the IN-clause helper.
        clause, p = _in_clause("answer_status", f["status"])
        if clause:
            clauses.append(clause)
            params.extend(p)
    for fkey, col in (("from", "date_asked"), ("from_ans", "date_answered")):
        v = f.get(fkey, "")
        if v:
            try:
                date.fromisoformat(v)
                clauses.append(f"{col} >= ?")
                params.append(v)
            except ValueError:
                pass
    for fkey, col in (("to", "date_asked"), ("to_ans", "date_answered")):
        v = f.get(fkey, "")
        if v:
            try:
                date.fromisoformat(v)
                clauses.append(f"{col} <= ?")
                params.append(v)
            except ValueError:
                pass
    # Text-substring filters on free-form columns (case-insensitive).
    for fkey, col in (("pqref", "pq_ref"), ("constit", "constituent"), ("notes", "notes")):
        v = f.get(fkey, "")
        if v:
            clauses.append(f"LOWER({col}) LIKE ?")
            params.append(f"%{v.lower()}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


@app.route("/")
def list_view():
    f = _parse_filters(request.args)
    page = max(1, int(request.args.get("page", 1)))
    offset = (page - 1) * PAGE_SIZE
    where, params = _build_where(f)
    sort_col = _SORT_COLS.get(f["sort"], "")
    sort_dir = f["dir"].upper() if f["dir"] in ("asc", "desc") else "DESC"
    grouped = f["group"]
    with _conn() as conn:
        search_refs, score_map = _search_prefilter(conn, f)
        # Build the matched-row set the same way regardless of grouping: when
        # search is on, narrow to ranked candidates; otherwise apply WHERE.
        # In grouped mode we *also* need to pull in the matched rows' siblings
        # so a group surfaces even when the canonical itself was filtered out.
        if search_refs is not None:
            if not search_refs:
                base_rows: list = []
            else:
                placeholders = ",".join("?" * len(search_refs))
                where_combined = where + (" AND " if where else " WHERE ") + f"pq_ref IN ({placeholders})"
                base_rows = conn.execute(
                    f"SELECT * FROM questions{where_combined}",
                    params + list(search_refs),
                ).fetchall()
        else:
            base_rows = conn.execute(
                f"SELECT * FROM questions{where}", params,
            ).fetchall()

        if grouped:
            # Group ONLY the matching rows — don't pull in non-matching
            # siblings. A group surfaces as expandable only when ≥2 of its
            # members made it through the filter/search. If only one did, it
            # renders as a regular singleton (no twisty) — exactly as if it
            # were never grouped. The full Oireachtas grouping is still
            # discoverable via the modal's "shared with" panel.
            display = _collapse_groups(base_rows)
            # Sort the collapsed list. Sort precedence:
            #   1. explicit column sort (?sort=...) — by canonical's column
            #   2. search relevance — by canonical's score
            #   3. default: date_asked DESC then pq_ref
            if sort_col:
                display.sort(
                    key=lambda d: ((d.get(sort_col) is None), d.get(sort_col) or ""),
                    reverse=(sort_dir == "DESC"),
                )
            elif search_refs is not None:
                # Group's score = max score across its in-corpus members so a
                # group with one strong-matching member ranks above another
                # whose only matches are weak.
                def group_score(d: dict) -> float:
                    refs = [d["pq_ref"]] + [m["pq_ref"] for m in d["group_members"]]
                    return max((score_map.get(r, 0.0) for r in refs), default=0.0)
                display.sort(key=lambda d: -group_score(d))
            else:
                display.sort(
                    key=lambda d: (d.get("date_asked") or "", d["pq_ref"]),
                    reverse=True,
                )
            total = len(display)
            rows = display[offset:offset + PAGE_SIZE]
        else:
            # Flat (ungrouped) mode — original behaviour.
            if search_refs is not None:
                if not base_rows:
                    rows = []
                    total = 0
                else:
                    rows_all = list(base_rows)
                    if sort_col:
                        rows_all = sorted(
                            rows_all,
                            key=lambda r: ((r[sort_col] is None), r[sort_col] or ""),
                            reverse=(sort_dir == "DESC"),
                        )
                    else:
                        rows_all.sort(key=lambda r: -score_map.get(r["pq_ref"], 0.0))
                    total = len(rows_all)
                    rows = rows_all[offset:offset + PAGE_SIZE]
            else:
                total = len(base_rows)
                # Re-issue with ORDER BY + LIMIT to leverage SQLite indexes
                # rather than pulling everything into Python for the unfiltered
                # default view.
                order_by = (
                    f"{sort_col} {sort_dir}, pq_ref" if sort_col
                    else "date_asked DESC, pq_ref"
                )
                rows = conn.execute(
                    f"SELECT * FROM questions{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                    params + [PAGE_SIZE, offset],
                ).fetchall()
        tds = db.distinct_values(conn, "td_name")
        parties = db.distinct_values(conn, "td_party")
        constituencies = db.distinct_values(conn, "td_constituency")
        depts = db.distinct_values(conn, "department")
        topics = _all_topics(conn)
        all_tags_list = db.all_tags(conn)
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE answer_status='pending'"
        ).fetchone()[0]
        total_in_db = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    with _conn() as conn2:
        pq_tags_map = db.pq_tags_by_pqref(conn2)
        hse_pdf_map: dict[str, int] = {}
        for r2 in conn2.execute(
            """SELECT j.pq_ref, MIN(p.id) AS pdf_id
                 FROM hse_pdf_pqs j
                 JOIN hse_pdfs p ON p.id = j.hse_pdf_id
                WHERE p.local_path IS NOT NULL
                GROUP BY j.pq_ref"""
        ):
            hse_pdf_map[r2["pq_ref"]] = r2["pdf_id"]
    def _enrich(d: dict) -> dict:
        d["pq_tags"] = pq_tags_map.get(d["pq_ref"], [])
        d["hse_pdf_id"] = hse_pdf_map.get(d["pq_ref"])
        return d

    enriched = []
    for r in rows:
        # In grouped mode, `r` is already a dict (from _collapse_groups). Its
        # `group_members` (if any) also need tag + HSE enrichment so siblings
        # render with the same affordances as their canonical when expanded.
        # In flat mode, `r` is a sqlite3.Row that still needs _row_to_dict.
        d = r if isinstance(r, dict) else _row_to_dict(r)
        _enrich(d)
        for sib in d.get("group_members", []) or []:
            if isinstance(sib, dict):
                _enrich(sib)
        enriched.append(d)
    return render_template(
        "list.html",
        rows=enriched,
        total=total,
        total_in_db=total_in_db,
        pending_count=pending_count,
        page=page,
        total_pages=total_pages,
        page_size=PAGE_SIZE,
        f=f,
        tds=tds,
        parties=parties,
        constituencies=constituencies,
        depts=depts,
        topics=topics,
        all_tags=all_tags_list,
    )


@app.route("/pq/<path:pq_ref>")
def detail_view(pq_ref: str):
    with _conn() as conn:
        row = db.get_question(conn, pq_ref)
        if row is None:
            abort(404)
        d = _row_to_dict(row, conn)
        all_tags_list = db.all_tags(conn)
    return render_template("detail.html", row=d, all_tags=all_tags_list)


@app.route("/pq/<path:pq_ref>/save", methods=["POST"])
def save(pq_ref: str):
    """Classic form POST from the detail page. Redirects back on success."""
    from .matching import match_keyword_tags
    constituent = (request.form.get("constituent") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None
    hse_pdf_url = (request.form.get("hse_pdf_url") or "").strip() or None
    tags_csv = (request.form.get("tags") or "").strip()
    tags = [t.strip() for t in tags_csv.split(",")] if tags_csv else []
    keywords = cfg.load_config().keywords
    with _conn() as conn:
        if not db.update_manual_fields(conn, pq_ref, constituent, notes, hse_pdf_url):
            abort(404)
        row = db.get_question(conn, pq_ref)
        text = "\n".join(filter(None, [row["raw_question_showas"], row["question_text"]]))
        auto = match_keyword_tags(text, keywords)
        db.set_desired_tags(conn, pq_ref, tags, auto)
        conn.commit()
    return redirect(url_for("detail_view", pq_ref=pq_ref))


@app.route("/api/pq/<path:pq_ref>", methods=["GET"])
def api_get(pq_ref: str):
    """Return the full row as JSON for the list-view modal.

    Two sources can populate `question_html` / `answer_html` in the response:

      1. **Search highlighting** (takes precedence): when ?q=... is set and the
         lexical index is in use (?i=lex, default), FTS5 `highlight()` wraps
         each stemmed hit in <mark> over the plain-text columns.
      2. **Rich rendering from XML**: when no highlight applies, render HTML
         directly from the stored Akoma Ntoso XML (`xml_raw`). Preserves
         paragraphs, tables, lists — what `question_text` / `answer_text`
         flatten away.

    The `xml_raw` BLOB itself is stripped from the JSON response (it's bytes,
    not JSON-serialisable, and the client doesn't need it).
    """
    from . import akoma_html
    q = (request.args.get("q") or "").strip()
    domains = set(_multi(request.args, "d")) or {"question", "answer"}
    indexes = set(_multi(request.args, "i")) or {"lex", "sem"}
    with _conn() as conn:
        row = db.get_question(conn, pq_ref)
        if row is None:
            abort(404)
        d = _row_to_dict(row, conn, with_shared=True)
        if q and "lex" in indexes:
            match_expr = _fts_escape(q)
            if match_expr:
                hl = conn.execute(
                    """SELECT highlight(questions_fts, 0, '<mark>', '</mark>') AS hq,
                              highlight(questions_fts, 1, '<mark>', '</mark>') AS ha
                         FROM questions_fts
                        WHERE questions_fts MATCH ?
                          AND questions_fts.pq_ref = ?""",
                    (match_expr, pq_ref),
                ).fetchone()
                if hl is not None:
                    if "question" in domains and "<mark>" in (hl["hq"] or ""):
                        d["question_html"] = _strip_question_boilerplate(hl["hq"])
                    if "answer" in domains and "<mark>" in (hl["ha"] or ""):
                        d["answer_html"] = hl["ha"]
    # Fetch xml_raw separately (it's stripped by _row_to_dict for JSON safety).
    # Only needed when at least one body still needs rendering.
    need_q = "question_html" not in d
    need_a = "answer_html" not in d
    if need_q or need_a:
        with _conn() as conn:
            xrow = conn.execute(
                "SELECT xml_raw FROM questions WHERE pq_ref = ?", (pq_ref,)
            ).fetchone()
        xml_raw = xrow["xml_raw"] if xrow else None
        if xml_raw:
            uri = d.get("question_uri") or ""
            e_id = uri.rsplit("/", 1)[-1] if uri else ""
            if e_id:
                q_html, a_html = akoma_html.render_question_and_answer(xml_raw, e_id)
                if q_html:
                    q_html = _strip_question_boilerplate(q_html)
                if q_html and need_q:
                    d["question_html"] = q_html
                if a_html and need_a:
                    d["answer_html"] = a_html
    return jsonify(d)


@app.route("/export.xlsx")
def export_xlsx():
    """Stream an .xlsx of every row matching the current list-view filters.

    Same filter+search semantics as the list view, but no pagination — the
    export contains every matching row. When a search query is active, rows
    are exported in score (relevance) order.
    """
    f = _parse_filters(request.args)
    where, params = _build_where(f)
    with _conn() as conn:
        search_refs, score_map = _search_prefilter(conn, f, limit=2000)
        if search_refs is not None:
            if not search_refs:
                rows = []
            else:
                placeholders = ",".join("?" * len(search_refs))
                where_combined = where + (" AND " if where else " WHERE ") + f"pq_ref IN ({placeholders})"
                rows_all = conn.execute(
                    f"SELECT * FROM questions{where_combined}", params + list(search_refs)
                ).fetchall()
                rows_all.sort(key=lambda r: -score_map.get(r["pq_ref"], 0.0))
                rows = rows_all
        else:
            rows = conn.execute(
                f"SELECT * FROM questions{where} ORDER BY date_asked DESC, pq_ref",
                params,
            ).fetchall()
        answers_map = db.answers_by_pqref(conn)
    buf = io.BytesIO()
    exports.write_xlsx(rows, buf, answers_map=answers_map)
    buf.seek(0)
    filtered = any(v for k, v in f.items())
    suffix = "filtered" if filtered else "all"
    filename = f"pqs-{_date.today().isoformat()}-{suffix}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/tags", methods=["GET"])
def api_tags():
    """Sorted list of every tag in use, for autocomplete in the modal."""
    with _conn() as conn:
        return jsonify(db.all_tags(conn))


@app.route("/settings", methods=["GET"])
def settings_view():
    settings = cfg.load_config()
    with _conn() as conn:
        counts = db.tag_counts(conn)
    return render_template(
        "settings.html",
        settings=settings,
        tag_counts=counts,
        saved=request.args.get("saved"),
    )


@app.route("/settings/keywords", methods=["POST"])
def settings_save_keywords():
    """Save topics.yaml from the form: search_terms[], keyword[], lookback_days.

    Returns JSON when called via fetch (X-Requested-With: fetch) so the two
    combined buttons ('Save & Refresh PQs', 'Save & Rebuild tags') can chain
    a follow-up call without a full page reload. Otherwise redirects back
    to /settings for the classic form-submit path.
    """

    def _dedupe(raw: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for k in raw:
            s = (k or "").strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    search_terms = _dedupe(request.form.getlist("search_term"))
    keywords = _dedupe(request.form.getlist("keyword"))
    if not search_terms:
        # An empty list would let every PQ in next ingest — guard explicitly
        # by falling back to the previous value rather than silently widening.
        search_terms = cfg.load_config().search_terms
    try:
        lookback = max(1, int(request.form.get("lookback_days", "90")))
    except ValueError:
        lookback = 90
    current = cfg.load_config()
    cfg.save_config(cfg.Config(
        search_terms=search_terms,
        keywords=keywords,
        lookback_days=lookback,
        chambers=current.chambers,
        xml_fetch_delay_ms=current.xml_fetch_delay_ms,
    ))
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({
            "ok": True,
            "search_terms": search_terms,
            "keywords": keywords,
            "lookback_days": lookback,
        })
    return redirect(url_for("settings_view", saved="keywords"))


# In-process state for the background ingest. The Flask app is single-process,
# so a module-level dict guarded by a lock is enough — no need for an external
# queue. The scheduled daily ingest runs in a separate Python process and
# competes for the SQLite write lock the normal way (WAL + busy_timeout).
_ingest_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "lookback_days": None,
    "result": None,        # dict from ingest.run() on success
    "error": None,         # str on failure
}
_ingest_lock = threading.Lock()


def _run_ingest_in_thread(lookback_days: int) -> None:
    from . import ingest
    from datetime import datetime as _dt
    try:
        result = ingest.run(lookback_days=lookback_days, log_to_file=True)
        with _ingest_lock:
            _ingest_state.update({
                "running": False,
                "finished_at": _dt.utcnow().isoformat(timespec="seconds"),
                "result": result,
            })
    except Exception as e:  # noqa: BLE001
        log_app(f"background ingest failed: {e}")
        with _ingest_lock:
            _ingest_state.update({
                "running": False,
                "finished_at": _dt.utcnow().isoformat(timespec="seconds"),
                "error": str(e),
            })


@app.route("/api/ingest/start", methods=["POST"])
def api_ingest_start():
    """Kick off a fresh Oireachtas ingest in a background thread.

    Uses ``lookback_days`` from the just-saved topics.yaml. Refuses if an
    ingest is already in flight (409). The follow-up poll is
    ``GET /api/ingest/status``.
    """
    from datetime import datetime as _dt
    with _ingest_lock:
        if _ingest_state["running"]:
            return jsonify({
                "ok": False,
                "error": "ingest already running",
                "started_at": _ingest_state["started_at"],
            }), 409
        lookback = cfg.load_config().lookback_days
        _ingest_state.update({
            "running": True,
            "started_at": _dt.utcnow().isoformat(timespec="seconds"),
            "finished_at": None,
            "lookback_days": lookback,
            "result": None,
            "error": None,
        })
    t = threading.Thread(
        target=_run_ingest_in_thread, args=(lookback,), daemon=True,
        name=f"ingest-lookback{lookback}",
    )
    t.start()
    return jsonify({"ok": True, "started": True, "lookback_days": lookback})


@app.route("/api/ingest/status", methods=["GET"])
def api_ingest_status():
    """Current state of the background ingest. Snapshot of the module dict."""
    with _ingest_lock:
        return jsonify(dict(_ingest_state))


@app.route("/api/topics/rebuild", methods=["POST"])
def api_topics_rebuild():
    """Recompute auto tags for every PQ against the current topics.yaml.

    Pure local rescan — no Oireachtas network calls. Preserves user overrides:
    a tag the user explicitly removed (user_suppressed) stays suppressed; a
    tag the user added (user_added) stays added. Runs in a single transaction;
    a few seconds for the 2k-row corpus.

    Returns counts of PQs whose display tag set changed plus per-state totals.
    """
    from .matching import match_keyword_tags
    keywords = cfg.load_config().keywords
    scanned = 0
    updated = 0
    cleared = 0
    with _conn() as conn:
        rows = conn.execute(
            "SELECT pq_ref, raw_question_showas, question_text, matched_topics FROM questions"
        ).fetchall()
        for r in rows:
            scanned += 1
            text = "\n".join(filter(None, [r["raw_question_showas"], r["question_text"]]))
            hits = match_keyword_tags(text, keywords)
            try:
                prior_display = set(json.loads(r["matched_topics"] or "[]"))
            except json.JSONDecodeError:
                prior_display = set()
            new_display = db.apply_auto_tags(conn, r["pq_ref"], hits)
            if set(new_display) != prior_display:
                updated += 1
                if not new_display:
                    cleared += 1
        conn.commit()
    return jsonify({
        "ok": True,
        "scanned": scanned,
        "updated": updated,
        "cleared": cleared,
        "keywords": keywords,
    })


@app.route("/api/tag", methods=["POST"])
def api_tag_admin():
    """Tag-admin actions from the settings page.

    Body JSON: {"action": "rename"|"delete", "tag": "...", "new": "..."}
    """
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").lower()
    tag = (payload.get("tag") or "").strip()
    new = (payload.get("new") or "").strip()
    if not tag or action not in {"rename", "delete"}:
        return jsonify({"ok": False, "error": "bad request"}), 400
    with _conn() as conn:
        if action == "rename":
            if not new:
                return jsonify({"ok": False, "error": "new name required"}), 400
            n = db.rename_tag(conn, tag, new)
        else:
            n = db.delete_tag(conn, tag)
        conn.commit()
    return jsonify({"ok": True, "affected": n})


@app.route("/api/pq/<path:pq_ref>", methods=["POST"])
def api_save(pq_ref: str):
    """AJAX save for the modal. Body: JSON {constituent, notes, hse_pdf_url}.

    A long-running ingest holds the write lock between batched commits. We
    retry briefly so saves succeed even when a backfill is in progress.
    """
    import time
    payload = request.get_json(silent=True) or {}
    constituent = (payload.get("constituent") or "").strip() or None
    notes = (payload.get("notes") or "").strip() or None
    # hse_pdf_url is no longer surfaced in the modal; preserve the DB value if
    # the key is omitted (manual-fields-never-overwritten contract).
    hse_pdf_url_provided = "hse_pdf_url" in payload
    hse_pdf_url = (payload.get("hse_pdf_url") or "").strip() or None
    tags = payload.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    from .matching import match_keyword_tags
    keywords = cfg.load_config().keywords
    last_err: Exception | None = None
    for attempt in range(20):
        try:
            with _conn() as conn:
                if not hse_pdf_url_provided:
                    existing = db.get_question(conn, pq_ref)
                    if existing is None:
                        abort(404)
                    hse_pdf_url = existing["hse_pdf_url"]
                if not db.update_manual_fields(conn, pq_ref, constituent, notes, hse_pdf_url):
                    abort(404)
                row = db.get_question(conn, pq_ref)
                text = "\n".join(filter(None, [row["raw_question_showas"], row["question_text"]]))
                auto = match_keyword_tags(text, keywords)
                db.set_desired_tags(conn, pq_ref, tags, auto)
                conn.commit()
                row = db.get_question(conn, pq_ref)
                d = _row_to_dict(row, conn)
            return jsonify({"ok": True, "row": d})
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last_err = e
            time.sleep(0.5 + attempt * 0.25)  # 0.5s, 0.75s, 1s, ... up to ~5.5s
    return jsonify({"ok": False, "error": f"DB busy after retries: {last_err}"}), 503


def _full_group_size(group_key: str | None) -> int:
    """Count the eIds in a stored group key. Returns 1 for singletons / NULL."""
    if not group_key or "|" not in group_key:
        return 1
    try:
        return len(group_key.split("|", 1)[1].split(","))
    except (IndexError, ValueError):
        return 1


def _agenda_num(row_or_dict) -> int:
    """Numeric agenda number from ``question_uri`` for canonical-row picks."""
    uri = row_or_dict["question_uri"] if row_or_dict["question_uri"] else ""
    eid = uri.rsplit("/", 1)[-1]
    if eid.startswith("pq_"):
        try:
            return int(eid[3:])
        except ValueError:
            pass
    return 10**9


def _collapse_groups(rows: list) -> list[dict]:
    """Collapse rows that share a ``question_group_key`` to one display dict.

    Each collapsed dict carries:
      - all _row_to_dict fields of the canonical (lowest-agenda-number) member
      - ``group_full_size``: total members per the Oireachtas XML (covers
        siblings not in our corpus too — read from the key)
      - ``group_in_corpus_size``: how many of those members are in our DB
        AND match the current filter set (the rows we were handed)
      - ``group_members``: list of FULL _row_to_dict dicts for the
        OTHER (non-canonical) in-corpus siblings — same shape as canonical,
        so the template can render them as inline expand-rows. The caller
        is responsible for enriching `tags` / `hse_pdf_id` on both canonical
        and siblings (since those need DB lookups outside this function).

    Rows without a ``question_group_key`` stand alone (treated as singletons).
    """
    by_key: dict[str, list] = {}
    for r in rows:
        k = r["question_group_key"]
        bucket = k if k else f"_solo:{r['pq_ref']}"
        by_key.setdefault(bucket, []).append(r)
    out: list[dict] = []
    for bucket, members in by_key.items():
        members.sort(key=_agenda_num)
        d = _row_to_dict(members[0])
        if bucket.startswith("_solo:"):
            d["group_full_size"] = 1
            d["group_in_corpus_size"] = 1
            d["group_members"] = []
        else:
            d["group_full_size"] = _full_group_size(bucket)
            d["group_in_corpus_size"] = len(members)
            d["group_members"] = [_row_to_dict(m) for m in members[1:]]
        out.append(d)
    return out


def _row_to_dict(row: sqlite3.Row, conn: sqlite3.Connection | None = None,
                 *, with_shared: bool = False) -> dict:
    d = dict(row)
    # xml_raw is BLOB — not JSON-serialisable and the client doesn't need it
    # in shared responses. Callers that DO need it (modal-render only) fetch
    # the bytes via a dedicated query.
    d.pop("xml_raw", None)
    # Strip the redundant "{NUM}. Deputy X asked the Minister for Y" prefix
    # for display — TD + Minister are already on the modal summary strip.
    if d.get("question_text"):
        d["question_text"] = _strip_question_boilerplate(d["question_text"])
    # answer_text lives on `answers`, joined via answer_id. Inject here so
    # downstream callers (JSON modal, detail.html, markdown export) keep
    # seeing `d["answer_text"]` without each one repeating the JOIN.
    if conn is not None and d.get("answer_id"):
        ans = conn.execute(
            "SELECT answer_text FROM answers WHERE id = ?", (d["answer_id"],)
        ).fetchone()
        d["answer_text"] = ans["answer_text"] if ans else None
    else:
        d["answer_text"] = None
    try:
        d["matched_topics_list"] = json.loads(d.get("matched_topics") or "[]")
    except json.JSONDecodeError:
        d["matched_topics_list"] = []
    # Full Oireachtas group size for this PQ (= number of eIds in the shared
    # XML, or 1 for singletons). Useful for the list-view "+N grouped" chip
    # even when the grouping toggle is off — surfaces that the row is part
    # of a wider group regardless of how it's being rendered. The list-view
    # collapse path overwrites `group_in_corpus_size` and `group_members`.
    d["group_full_size"] = _full_group_size(d.get("question_group_key"))
    d["group_in_corpus_size"] = 1
    d["group_members"] = []
    if conn is not None:
        # Unified tag set: (auto + user_added) with per-tag state so the
        # template can colour chips (auto → blue, user_added → yellow).
        pq_tag_rows = db.get_pq_tags(conn, d["pq_ref"])
        d["pq_tags"] = pq_tag_rows
        d["tags"] = [r["tag"] for r in pq_tag_rows]  # back-compat for list rendering
        d["hse_pdfs"] = db.get_hse_pdfs_for_pq(conn, d["pq_ref"])
        if with_shared:
            group = db.get_group_siblings(conn, d["pq_ref"])
            d["shared_with"] = group["siblings"]
            d["shared_total"] = group["total"]  # includes the current PQ
        else:
            d["shared_with"] = []
            d["shared_total"] = 0
    else:
        d["pq_tags"] = []
        d["tags"] = []
        d["hse_pdfs"] = []
        d["shared_with"] = []
        d["shared_total"] = 0
    return d


# --- search ---

# Cache the embedding matrix in-process so semantic search isn't reloading
# ~10MB of float32 from SQLite on every request. Invalidated by the indexer
# bumping `embeddings_loaded_at` (we re-check the row count cheaply).
_emb_cache: dict = {"matrix": None, "meta": None, "source_type": None, "n_rows": -1}


def _emb_load(conn, source_type: str):
    """Return (meta, matrix) for a source_type, using a cheap freshness check."""
    from . import embeddings as emb_mod
    n_rows = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE source_type = ?", (source_type,)
    ).fetchone()[0]
    if (_emb_cache.get("source_type") == source_type
            and _emb_cache.get("n_rows") == n_rows
            and _emb_cache.get("matrix") is not None):
        return _emb_cache["meta"], _emb_cache["matrix"]
    meta, matrix = emb_mod.load_matrix(conn, source_type=source_type)
    _emb_cache.update({"meta": meta, "matrix": matrix,
                       "source_type": source_type, "n_rows": n_rows})
    return meta, matrix


def _fts_escape(s: str) -> str:
    """Build an FTS5 MATCH expression from user input.

    Splits on whitespace (respecting double-quoted phrases), AND-joins each
    token, and supports trailing wildcards (`diab*`). FTS5 syntax characters
    in user input are escaped by quoting.

      "diabetes"               → "diabetes"
      diab*                    → "diab"*
      mental health            → "mental" AND "health"
      "mental health" waiting  → "mental health" AND "waiting"
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
            escaped = t.replace('"', '""')
            parts.append(f'"{escaped}"')
    return " AND ".join(parts)


def _build_column_match(escape_fn, q_text: str, a_text: str) -> str:
    """Combine per-field user input into a single FTS5 MATCH expression.

    Returns '' if no usable input remains after escaping (e.g. trigram needs
    >=3 chars and the user typed something shorter).
    """
    clauses: list[str] = []
    if q_text:
        expr = escape_fn(q_text)
        if expr:
            clauses.append(f'{{question_text}}: {expr}')
    if a_text:
        expr = escape_fn(a_text)
        if expr:
            clauses.append(f'{{answer_text}}: {expr}')
    return " AND ".join(clauses)


def _query_fts(conn, table: str, match_expr: str, limit: int) -> list:
    """Run one FTS5 table's MATCH and return rows ordered by bm25() ascending."""
    if not match_expr:
        return []
    return conn.execute(
        f"""SELECT q.pq_ref, q.date_asked, q.td_name, q.td_party, q.department,
                   q.matched_topics, q.answer_status,
                   bm25({table}) AS score,
                   snippet({table}, 0, '<mark>', '</mark>', '...', 12) AS snippet_q,
                   snippet({table}, 1, '<mark>', '</mark>', '...', 12) AS snippet_a
              FROM {table}
              JOIN questions q ON q.pq_ref = {table}.pq_ref
             WHERE {table} MATCH ?
             ORDER BY score
             LIMIT ?""",
        (match_expr, limit),
    ).fetchall()


def _rrf_merge(*ranked_lists, k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion across N already-ranked result lists.

    Each list is a sequence of rows with a 'pq_ref' key. Earlier position = better.
    Returns {pq_ref: rrf_score} where higher = better. Standard k=60.
    """
    scores: dict[str, float] = {}
    for rows in ranked_lists:
        for rank, r in enumerate(rows):
            pq = r["pq_ref"]
            scores[pq] = scores.get(pq, 0.0) + 1.0 / (k + rank + 1)
    return scores


@app.route("/api/search/bm25", methods=["GET"])
def api_search_bm25():
    """Lexical search over the porter-stemmed FTS5 index. Per-field queries
    via ?q_text=... and ?a_text=...

    User input supports:
      - stemmed tokens (diabetes ↔ diabetic, waiting ↔ waited)
      - quoted phrases: "home help hours"
      - trailing wildcards: diab*
    """
    q_text = (request.args.get("q_text") or "").strip()
    a_text = (request.args.get("a_text") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        limit = 50
    if not (q_text or a_text):
        return jsonify({"ok": True, "results": [], "note": "no query"})

    match_expr = _build_column_match(_fts_escape, q_text, a_text)
    if not match_expr:
        return jsonify({"ok": True, "results": [], "note": "empty after escape"})

    with _conn() as conn:
        rows = _query_fts(conn, "questions_fts", match_expr, limit)

    out = []
    for r in rows:
        out.append({
            "pq_ref": r["pq_ref"],
            "score": r["score"],   # SQLite BM25: lower = better
            "date_asked": r["date_asked"],
            "td_name": r["td_name"],
            "department": r["department"],
            "matched_topics": json.loads(r["matched_topics"] or "[]"),
            "answer_status": r["answer_status"],
            "snippet_question": r["snippet_q"],
            "snippet_answer": r["snippet_a"],
        })
    return jsonify({
        "ok": True,
        "match_expr": match_expr,
        "results": out,
    })


@app.route("/api/search/semantic", methods=["GET"])
def api_search_semantic():
    """Semantic search over question/answer embeddings.
       ?q=...        embed once, score against BOTH question + answer matrices,
                     take per-PQ max(cos_q, cos_a)
       ?q_text=...   score against question embeddings only
       ?a_text=...   score against answer embeddings only
    """
    from . import embeddings as emb_mod
    q = (request.args.get("q") or "").strip()
    q_text = (request.args.get("q_text") or "").strip()
    a_text = (request.args.get("a_text") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        limit = 50
    if not (q or q_text or a_text):
        return jsonify({"ok": True, "results": [], "note": "no query"})

    # Embed once per distinct text. Keep it simple: one query, scored
    # against whichever matrices the caller asked for.
    queries: list[tuple[str, str]] = []  # (source_type, text)
    if q_text:
        queries.append(("question", q_text))
    if a_text:
        queries.append(("answer", a_text))
    if q and not (q_text or a_text):
        queries.append(("question", q))
        queries.append(("answer", q))

    # Embed all queries in one batch.
    texts = [t for _, t in queries]
    try:
        qvecs = emb_mod.embed_texts(texts)
    except Exception as e:
        return jsonify({"ok": False, "error": f"embedding failed: {e}"}), 500

    # Per-PQ best score across all selected fields.
    best: dict[str, dict] = {}
    with _conn() as conn:
        for (source_type, _), qvec in zip(queries, qvecs):
            meta, matrix = _emb_load(conn, source_type)
            if matrix.shape[0] == 0:
                continue
            order, scores = emb_mod.cosine_topk(qvec, matrix, k=limit * 4)
            for rank_i, (idx, sc) in enumerate(zip(order, scores)):
                _, pq_ref, _, chunk_index, excerpt = meta[idx]
                if not pq_ref:
                    continue
                prev = best.get(pq_ref)
                if prev is None or sc > prev["score"]:
                    best[pq_ref] = {
                        "pq_ref": pq_ref, "score": float(sc),
                        "matched_field": source_type,
                        "matched_chunk": int(chunk_index),
                        "excerpt": excerpt[:300],
                    }
        if not best:
            return jsonify({"ok": True, "results": []})
        # Hydrate top results with question row data.
        top = sorted(best.values(), key=lambda x: -x["score"])[:limit]
        refs = [t["pq_ref"] for t in top]
        placeholders = ",".join("?" * len(refs))
        rows = conn.execute(
            f"""SELECT pq_ref, date_asked, td_name, td_party, department,
                       matched_topics, answer_status
                  FROM questions WHERE pq_ref IN ({placeholders})""",
            refs,
        ).fetchall()
        by_ref = {r["pq_ref"]: dict(r) for r in rows}
    out = []
    for t in top:
        r = by_ref.get(t["pq_ref"], {})
        out.append({
            **t,
            "date_asked": r.get("date_asked"),
            "td_name": r.get("td_name"),
            "department": r.get("department"),
            "matched_topics": json.loads(r.get("matched_topics") or "[]"),
            "answer_status": r.get("answer_status"),
        })
    return jsonify({"ok": True, "results": out})


@app.route("/api/hse-pdf/<int:pdf_id>/text", methods=["GET"])
def api_hse_pdf_text(pdf_id: int):
    """Return extracted paragraph text for one HSE PDF.

    Optional ?q=... applies the same FTS5 escape as the main search and wraps
    each lexical match with <mark>...</mark>. When the index has no match for
    this PDF, paragraphs come back as plain text (the UI uses HTML escaping).

    Response shape:
      {
        ok: true,
        status: 'done'|'empty'|'failed'|'unprocessed',
        page_count: int|null,
        paragraphs: [{id, page_no, para_index, text, html?}, ...],
      }
    """
    q = (request.args.get("q") or "").strip()
    with _conn() as conn:
        meta = conn.execute(
            "SELECT id, text_extraction_status, text_page_count, ocr_used "
            "  FROM hse_pdfs WHERE id = ?", (pdf_id,)
        ).fetchone()
        if meta is None:
            abort(404)
        status = meta["text_extraction_status"] or "unprocessed"
        ocr_used = bool(meta["ocr_used"])
        rows = conn.execute(
            "SELECT id, page_no, para_index, text FROM hse_paragraphs "
            "  WHERE hse_pdf_id = ? ORDER BY page_no, para_index",
            (pdf_id,),
        ).fetchall()
        # Build paragraph_id → highlighted HTML when a search is active.
        highlights: dict[int, str] = {}
        if q and rows:
            expr = _fts_escape(q)
            if expr:
                try:
                    hl_rows = conn.execute(
                        """SELECT hse_paragraphs_fts.rowid AS para_id,
                                  highlight(hse_paragraphs_fts, 0, '<mark>', '</mark>') AS html
                             FROM hse_paragraphs_fts
                             JOIN hse_paragraphs hp ON hp.id = hse_paragraphs_fts.rowid
                            WHERE hse_paragraphs_fts MATCH ?
                              AND hp.hse_pdf_id = ?""",
                        (expr, pdf_id),
                    ).fetchall()
                    for hl in hl_rows:
                        if hl["html"] and "<mark>" in hl["html"]:
                            highlights[hl["para_id"]] = hl["html"]
                except sqlite3.OperationalError as e:
                    log_app(f"HSE FTS highlight failed pdf_id={pdf_id}: {e}")
    paragraphs = []
    for r in rows:
        item = {"id": r["id"], "page_no": r["page_no"],
                "para_index": r["para_index"], "text": r["text"]}
        if r["id"] in highlights:
            item["html"] = highlights[r["id"]]
        paragraphs.append(item)
    return jsonify({
        "ok": True,
        "status": status,
        "page_count": meta["text_page_count"],
        "ocr_used": ocr_used,
        "paragraphs": paragraphs,
        "has_highlights": bool(highlights),
    })


@app.route("/hse-pdf/<int:pdf_id>")
def serve_hse_pdf(pdf_id: int):
    """Serve a downloaded HSE PDF from the local store."""
    with _conn() as conn:
        row = db.hse_pdf_by_id(conn, pdf_id)
    if row is None or not row["local_path"]:
        abort(404)
    # local_path is stored relative to cfg.ROOT with forward slashes.
    p = (cfg.ROOT / row["local_path"]).resolve()
    # Containment check: refuse to serve anything outside HSE_PDF_DIR.
    try:
        p.relative_to(cfg.HSE_PDF_DIR.resolve())
    except ValueError:
        abort(404)
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="application/pdf",
                     as_attachment=False, download_name=row["filename"])


def main() -> int:
    parser = argparse.ArgumentParser(prog="pq-tracker-ui",
                                     description="Local browser UI for the pq-tracker corpus.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5454)
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-open the browser.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    cfg.ensure_dirs()
    # Apply any pending schema migrations (e.g. new manual-edit columns)
    # so the UI works even if the user hasn't run an ingest since the upgrade.
    with _conn() as _c:
        db.init_schema(_c)
        _c.commit()
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")).start()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
