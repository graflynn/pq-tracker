"""MCP stdio server exposing the /api/v1 endpoints as agent tools.

Each tool is a thin shim around an HTTP call to the local Flask app
(default http://127.0.0.1:5454/api/v1). The Flask app must be running —
start-server.cmd handles that.

Env vars:
    PQ_API_BASE   override the API URL (rarely needed)
    PQ_API_TOKEN  if set, sent as Bearer; must match what Flask is checking

Invoked by Claude Code via the plugin's .mcp.json. To run by hand for
debugging: `python -m pq_tracker.mcp_server` and pipe a JSON-RPC request
to stdin.
"""
import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("PQ_API_BASE", "http://127.0.0.1:5454/api/v1")
TOKEN = (os.environ.get("PQ_API_TOKEN") or "").strip()

# (connect_timeout, read_timeout). Connect failure (server down) returns in
# under a second; read timeout is generous for /semantic which loads the
# embedding matrix on the first call.
_TIMEOUT = (3, 60)

mcp = FastMCP("pq-tracker")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def _server_down_message() -> str:
    return (
        f"The PQ tracker Flask server is not reachable at {API_BASE}. "
        "Please ask the user to double-click the 'PQ Server!' shortcut on "
        "their desktop (it opens a console window with the live logs), wait "
        "a few seconds for Flask to bind to port 5454, then retry the tool "
        "call. If the shortcut is missing, the launcher is at "
        "C:\\Users\\Grainne\\Documents\\pq-tracker\\start-server.cmd."
    )


def _get(path: str, **params: Any) -> dict:
    # Encode multi-value params as repeated query keys — Flask uses
    # request.args.getlist on the receiving side.
    qs: list[tuple[str, str]] = []
    for k, v in params.items():
        if v is None or v == "":
            continue
        if isinstance(v, list):
            for item in v:
                if item is not None and item != "":
                    qs.append((k, str(item)))
        else:
            qs.append((k, str(v)))
    try:
        r = requests.get(f"{API_BASE}{path}", params=qs, headers=_headers(), timeout=_TIMEOUT)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        raise RuntimeError(_server_down_message()) from None
    if r.status_code >= 500:
        r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    try:
        r = requests.post(f"{API_BASE}{path}", json=body, headers=_headers(), timeout=_TIMEOUT)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        raise RuntimeError(_server_down_message()) from None
    # 4xx bodies carry useful error messages (e.g. bad SQL) — pass them through.
    if r.status_code >= 500:
        r.raise_for_status()
    return r.json()


@mcp.tool()
def pq_facets() -> dict:
    """Discover what's queryable: distinct constituencies, parties, ministers,
    departments, members, auto-tags, manual-tags, date range, total PQ count.

    Call this FIRST when the user names something you'd need to spell exactly
    (a constituency, a TD, a minister title, a tag). Cheap (<10ms).
    """
    return _get("/facets")


@mcp.tool()
def pq_list(
    q: str = "",
    constituency: list[str] | None = None,
    member: list[str] | None = None,
    party: list[str] | None = None,
    minister: list[str] | None = None,
    department: list[str] | None = None,
    tag: list[str] | None = None,
    status: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    date_answered_from: str = "",
    date_answered_to: str = "",
    limit: int = 50,
    offset: int = 0,
    compact: bool = False,
    fields: list[str] | None = None,
) -> dict:
    """List/filter parliamentary questions with pagination.

    When `q` is given, results are FTS5-ranked (BM25). Otherwise sorted by
    date_asked DESC.

    All filter args AND together. Multi-value args (e.g. ["Donegal","Clare"])
    OR-match within themselves.

    Returns compact rows (snippet, not full text). For the full question +
    answer body, follow up with pq_get on the pq_ref.

    PAYLOAD-SIZE TIP. The default row carries ~12 fields per PQ; with
    limit=100+ that can blow past the agent's tool-output cap (see ISSUES.md).
    Use `compact=True` to drop tags/minister/department/permalink and halve
    the snippet, or `fields=[...]` to whitelist exactly the columns you need
    for downstream grouping. pq_ref is always returned. Prefer `fields=` over
    `pq_sql` when you just need a few columns for client-side aggregation.

    Args:
        q: FTS5 search; phrases ("mental health") and prefix * ("diab*") work.
        constituency: TD constituency (e.g. ["Donegal"]).
        member: TD name as it appears in the data.
        party: TD party (e.g. ["Sinn Féin"]).
        minister: minister name (free text, can be partial).
        department: department (almost always ["Health"]).
        tag: auto or manual tag (e.g. ["cgm", "type 1"]).
        status: "pending" | "answered".
        date_from / date_to: YYYY-MM-DD; bounds on date_asked.
        date_answered_from / date_answered_to: bounds on date_answered.
        limit: 1..500, default 50.
        offset: pagination offset.
        compact: drop tags/minister/department/permalink; snippet ~120 chars.
        fields: whitelist response keys (pq_ref always kept).
            Examples: ["pq_ref", "constituency", "member"] for grouping;
            ["pq_ref", "date_asked", "snippet"] for a timeline scan.
    """
    return _get(
        "/pqs",
        q=q,
        constituency=constituency,
        member=member,
        party=party,
        minister=minister,
        department=department,
        tag=tag,
        status=status,
        **{
            "from": date_from,
            "to": date_to,
            "from_ans": date_answered_from,
            "to_ans": date_answered_to,
        },
        limit=limit,
        offset=offset,
        compact="true" if compact else "",
        fields=",".join(fields) if fields else "",
    )


@mcp.tool()
def pq_get(pq_ref: str, include: str = "question,answer,tags,hse_pdfs") -> dict:
    """Fetch the full content of one PQ by its pq_ref.

    `include` is a comma-separated subset of:
        question     full question_text
        answer       full answer_text (often the bulk of the data)
        tags         auto + manual tags with state
        hse_pdfs     metadata of any HSE supplementary PDFs linked to this PQ
        xml          akoma-ntoso-rendered HTML (heaviest; opt-in)

    Default: question,answer,tags,hse_pdfs.
    """
    return _get(f"/pq/{pq_ref}", include=include)


@mcp.tool()
def pq_aggregate(
    group_by: str,
    group_by_2: str = "",
    q: str = "",
    constituency: list[str] | None = None,
    member: list[str] | None = None,
    party: list[str] | None = None,
    minister: list[str] | None = None,
    department: list[str] | None = None,
    tag: list[str] | None = None,
    status: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 200,
) -> dict:
    """Count PQs grouped along one OR two dimensions. The workhorse for
    reports, trends, and heatmaps.

    group_by    ∈ {constituency, member, party, minister, department, status,
                   month, year, tag}.
    group_by_2  optional second axis from the same set. When set, buckets
                have shape {key, key_2, count} — i.e. a 2D matrix suitable
                for a heatmap. Cannot equal group_by. 'tag' cannot be on
                both axes simultaneously.

    Buckets: month → "YYYY-MM"; year → "YYYY".
    For group_by="tag" (or group_by_2="tag"), a PQ with N tags contributes
    to N cells along that axis — sum(counts) > total_pqs when 'tag' is in
    play. For every other dimension each PQ contributes to exactly one
    bucket per axis.

    All filters honored — combine to slice before counting.

    Examples:
        Topic trends by year for one constituency:
            group_by="year", tag=["cgm"], constituency=["Donegal"]
        Which TDs raise type-1 issues most:
            group_by="member", tag=["type 1"]
        Pending vs answered split for 2025:
            group_by="status", date_from="2025-01-01"
        Heatmap of CGM PQs by constituency × year:
            group_by="constituency", group_by_2="year", tag=["cgm"]
        Constituency × topic matrix:
            group_by="constituency", group_by_2="tag"
    """
    return _get(
        "/aggregate",
        group_by=group_by,
        group_by_2=group_by_2,
        q=q,
        constituency=constituency,
        member=member,
        party=party,
        minister=minister,
        department=department,
        tag=tag,
        status=status,
        **{"from": date_from, "to": date_to},
        limit=limit,
    )


@mcp.tool()
def pq_semantic_search(
    q: str,
    source: str = "question",
    limit: int = 20,
    constituency: list[str] | None = None,
    member: list[str] | None = None,
    party: list[str] | None = None,
    tag: list[str] | None = None,
    status: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Cosine top-k over local BGE-small embeddings. Use when lexical
    matching misses (paraphrases, synonyms, vague concepts).

    source ∈ {question, answer, hse_paragraph}, default question.
    Filters apply to question/answer sources; ignored for hse_paragraph.

    Returns ranked items with a cosine score (higher = closer) and a
    240-char excerpt of the matching chunk.

    When to prefer this over pq_list:
        "cost burden on parents"      → semantic (no fixed phrase)
        "continuous glucose monitor"  → pq_list (exact term)
    """
    return _get(
        "/semantic",
        q=q,
        source=source,
        limit=limit,
        constituency=constituency,
        member=member,
        party=party,
        tag=tag,
        status=status,
        **{"from": date_from, "to": date_to},
    )


@mcp.tool()
def hse_list(
    source: list[str] | None = None,
    text_status: list[str] | None = None,
    pq_ref: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List HSE supplementary PDFs — these are the longer detailed replies
    HSE attaches when answering some PQs.

    Most useful: pq_ref="<ref>" to find PDFs linked to a specific PQ.
    Use text_status=["done"] to limit to PDFs whose text was extracted
    successfully (the rest are mostly images/scanned/corrupt).

    Returns metadata only. Full extracted paragraphs on hse_get.
    """
    return _get(
        "/hse_pdfs",
        source=source,
        text_status=text_status,
        pq_ref=pq_ref,
        **{"from": date_from, "to": date_to},
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def hse_get(pdf_id: int, include: str = "paragraphs,pqs") -> dict:
    """Fetch a single HSE PDF. `include` is a comma-separated subset of:
        paragraphs   structured (page, index, text) — cite a specific page
        text         single concatenated block — feed straight to the agent
        pqs          list of linked pq_refs

    Default: paragraphs,pqs.
    """
    return _get(f"/hse_pdf/{pdf_id}", include=include)


@mcp.tool()
def pq_sql(sql: str, params: list | None = None) -> dict:
    """Read-only SQL escape hatch when no curated tool fits.

    Connection is opened SQLite mode=ro — writes are physically impossible.
    Auto-injects LIMIT 1000 when none. ~8 second statement timeout.
    Multi-statement input rejected.

    Schema highlights (read-only, all of these are queryable):
        questions(pq_ref, date_asked, date_answered, td_name, td_party,
                  td_constituency, minister_name, department, question_text,
                  answer_status, answer_id, oireachtas_permalink, xml_url,
                  pdf_url, hse_pdf_url, constituent, notes, ...)
        answers(id, content_hash, answer_text)         -- q.answer_id → a.id
        pq_tags(pq_ref, tag, state)                    -- state: auto | user_added | user_suppressed
        questions_fts(question_text, answer_text, pq_ref)   -- FTS5 inline content
        hse_pdfs(id, source, source_url, publication_date, text_extraction_status,
                 text_page_count, ...)
        hse_pdf_pqs(hse_pdf_id, pq_ref)                -- many-to-many join
        hse_paragraphs(id, hse_pdf_id, page_no, para_index, text)
        embeddings(id, source_type, source_pq_ref, source_pdf_id,
                   chunk_index, text_excerpt, ...)     -- BGE-small vectors

    Use params=[...] for value binding rather than string-interpolating —
    safer and the only way to pass values containing quotes.
    """
    return _post("/sql", {"sql": sql, "params": params or []})


if __name__ == "__main__":
    mcp.run()
