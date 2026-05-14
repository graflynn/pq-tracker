"""Oireachtas Open Data API client.

Endpoints used:
  GET https://api.oireachtas.ie/v1/questions      -- index of Q+A pairs (JSON)
  GET https://api.oireachtas.ie/v1/members        -- member lookup for party/constituency
  GET https://data.oireachtas.ie/akn/...xml       -- Akoma Ntoso XML with full Q+A text
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterator

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

API_BASE = "https://api.oireachtas.ie/v1"
USER_AGENT = "pq-tracker/0.1 (+local research tool)"

PQ_REF_RE = re.compile(r"\[(\d+/\d+)\]")


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"] = "application/json"
    return s


@dataclass
class QuestionIndexEntry:
    """One row from /v1/questions JSON, normalised."""
    question_uri: str           # canonical URI ending in pq_<N>
    e_id: str                   # e.g. "pq_36"
    date: date
    chamber: str                # "dail"
    question_type: str          # "written"
    member_code: str
    member_name: str
    member_uri: str
    department: str             # to.showAs e.g. "Health"
    show_as: str                # full question text + PQ ref
    xml_url: str | None
    pdf_url: str | None
    debate_section_show_as: str
    debate_section_uri: str
    pq_ref: str | None          # extracted from show_as, e.g. "16475/26"


@dataclass
class QuestionAnswer:
    """Parsed Q+A from the Akoma Ntoso XML."""
    question_text: str
    answer_text: str | None
    minister_name: str | None
    is_answered: bool
    xml_raw: bytes | None = None  # raw XML bytes, for later rich-HTML rendering


@dataclass
class MemberRecord:
    member_code: str
    full_name: str
    parties_json: str    # JSON list of {"partyCode","start","end"}
    constituencies_json: str  # JSON list of {"constituency","start","end"}


class OireachtasClient:
    def __init__(self, fetch_delay_ms: int = 250):
        self.s = _session()
        self.delay_s = fetch_delay_ms / 1000.0

    # ---- questions index ----

    def peek_house_no(self, chamber: str, date_start: date, date_end: date,
                      qtype: str = "written") -> int | None:
        """Look up the current house number by fetching one question in the window."""
        params = {"qtype": qtype, "chamber": chamber,
                  "date_start": date_start.isoformat(),
                  "date_end": date_end.isoformat(),
                  "limit": 1, "skip": 0}
        r = self.s.get(f"{API_BASE}/questions", params=params, timeout=30)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        if not results:
            return None
        house = ((results[0].get("question") or {}).get("house") or {})
        try:
            return int(house.get("houseNo")) if house.get("houseNo") is not None else None
        except (TypeError, ValueError):
            return None

    def iter_questions(
        self,
        date_start: date,
        date_end: date,
        chamber: str = "dail",
        qtype: str = "written",
        page_size: int = 500,
        chunk_days: int = 14,
    ) -> Iterator[QuestionIndexEntry]:
        """Yield index entries across a date range.

        The API caps a single query at 10,000 results. We split the window into
        ``chunk_days`` slices so each query stays well under that ceiling.
        """
        from datetime import timedelta
        chunk_start = date_start
        while chunk_start <= date_end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), date_end)
            yield from self._iter_questions_chunk(chunk_start, chunk_end, chamber, qtype, page_size)
            chunk_start = chunk_end + timedelta(days=1)

    def _iter_questions_chunk(
        self,
        date_start: date,
        date_end: date,
        chamber: str,
        qtype: str,
        page_size: int,
    ) -> Iterator[QuestionIndexEntry]:
        skip = 0
        total: int | None = None
        while True:
            params = {
                "qtype": qtype,
                "chamber": chamber,
                "date_start": date_start.isoformat(),
                "date_end": date_end.isoformat(),
                "limit": page_size,
                "skip": skip,
            }
            url = f"{API_BASE}/questions"
            log.debug("GET %s %s", url, params)
            r = self.s.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            counts = (data.get("head") or {}).get("counts") or {}
            if total is None:
                total = int(counts.get("questionCount", 0))
                if total >= 10000:
                    log.warning("chunk %s..%s reports %d results (API cap is 10000) — consider smaller chunk_days",
                                date_start, date_end, total)
                else:
                    log.info("chunk %s..%s: %d questions [%s/%s]", date_start, date_end, total, chamber, qtype)
            results = data.get("results") or []
            if not results:
                return
            for r_ in results:
                entry = _parse_index_entry(r_)
                if entry is not None:
                    yield entry
            skip += len(results)
            if skip >= (total or 0):
                return

    # ---- members ----

    def fetch_house_members(self, chamber: str, house_no: int) -> list[MemberRecord]:
        """Pull all members belonging to a specific house (e.g. 34th Dáil = ~174 TDs).

        The /v1/members endpoint silently ignores per-member filters like member_id,
        but it respects chamber + house_no.  We page through with skip/limit.
        """
        out: list[MemberRecord] = []
        skip = 0
        page_size = 500
        total: int | None = None
        while True:
            url = f"{API_BASE}/members"
            params = {"chamber": chamber, "house_no": house_no,
                      "limit": page_size, "skip": skip}
            r = self.s.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            counts = (data.get("head") or {}).get("counts") or {}
            if total is None:
                total = int(counts.get("memberCount", 0))
                log.info("members index: %d total for %s house %s", total, chamber, house_no)
            results = data.get("results") or []
            if not results:
                break
            for r_ in results:
                rec = _parse_member(r_)
                if rec is not None:
                    out.append(rec)
            skip += len(results)
            if skip >= (total or 0):
                break
        return out

    # ---- XML payload ----

    def fetch_xml(self, xml_url: str, e_id: str) -> QuestionAnswer | None:
        time.sleep(self.delay_s)
        log.debug("GET %s", xml_url)
        r = self.s.get(xml_url, timeout=60, headers={"Accept": "application/xml"})
        if r.status_code != 200:
            log.warning("xml fetch %s -> %s", xml_url, r.status_code)
            return None
        try:
            root = etree.fromstring(r.content)
        except etree.XMLSyntaxError as e:
            log.warning("xml parse failed for %s: %s", xml_url, e)
            return None
        qa = _extract_qa(root, e_id)
        # Keep the raw bytes alongside the parsed text so the UI can re-render
        # rich HTML on demand without round-tripping through plain text.
        return QuestionAnswer(
            question_text=qa.question_text,
            answer_text=qa.answer_text,
            minister_name=qa.minister_name,
            is_answered=qa.is_answered,
            xml_raw=r.content,
        )


# -------- parsing helpers --------

def _parse_index_entry(row: dict) -> QuestionIndexEntry | None:
    q = row.get("question") or {}
    if not q:
        return None
    formats = ((q.get("debateSection") or {}).get("formats") or {})
    xml = (formats.get("xml") or {}).get("uri")
    pdf = (formats.get("pdf") or {}).get("uri") if isinstance(formats.get("pdf"), dict) else None
    show_as = (q.get("showAs") or "").strip()
    pq_match = PQ_REF_RE.search(show_as)
    pq_ref = pq_match.group(1) if pq_match else None
    by = q.get("by") or {}
    to = q.get("to") or {}
    house = q.get("house") or {}
    section = q.get("debateSection") or {}
    uri = q.get("uri") or ""
    e_id = uri.rsplit("/", 1)[-1] if uri else ""
    date_str = q.get("date")
    try:
        d = date.fromisoformat(date_str) if date_str else None
    except ValueError:
        d = None
    if d is None or not uri:
        return None
    return QuestionIndexEntry(
        question_uri=uri,
        e_id=e_id,
        date=d,
        chamber=(house.get("houseCode") or "").lower(),
        question_type=q.get("questionType") or "",
        member_code=(by.get("memberCode") or "").strip(),
        member_name=(by.get("showAs") or "").strip(),
        member_uri=(by.get("uri") or "").strip(),
        department=(to.get("showAs") or "").strip(),
        show_as=show_as,
        xml_url=xml,
        pdf_url=pdf,
        debate_section_show_as=(section.get("showAs") or "").strip(),
        debate_section_uri=(section.get("uri") or "").strip(),
        pq_ref=pq_ref,
    )


def _parse_member(member_block: dict) -> MemberRecord | None:
    import json
    m = member_block.get("member") or {}
    member_code = m.get("memberCode") or ""
    if not member_code:
        return None
    full_name = (m.get("fullName") or "").strip() or " ".join(
        s for s in [m.get("firstName"), m.get("lastName")] if s
    )
    parties: list[dict] = []
    constituencies: list[dict] = []
    for ms_wrap in (m.get("memberships") or []):
        ms = ms_wrap.get("membership") or ms_wrap
        date_range = ((ms.get("dateRange") or {}))
        start = date_range.get("start")
        end = date_range.get("end")
        for p_wrap in (ms.get("parties") or []):
            p = (p_wrap.get("party") or p_wrap)
            parties.append({
                "partyCode": p.get("partyCode") or p.get("showAs"),
                "showAs": p.get("showAs"),
                "start": start,
                "end": end,
            })
        rep = ms.get("represents") or []
        for r_wrap in rep:
            r = r_wrap.get("represent") or r_wrap.get("constituency") or r_wrap
            constituencies.append({
                "constituency": r.get("showAs") or r.get("constituencyName"),
                "start": start,
                "end": end,
            })
    return MemberRecord(
        member_code=member_code,
        full_name=full_name,
        parties_json=json.dumps(parties, ensure_ascii=False),
        constituencies_json=json.dumps(constituencies, ensure_ascii=False),
    )


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _text(elem: etree._Element) -> str:
    # Whitespace-collapsed concatenation of all descendant text.
    parts = elem.xpath(".//text()")
    out = " ".join(s.strip() for s in parts if s and s.strip())
    return out.strip()


def _extract_qa(root: etree._Element, e_id: str) -> QuestionAnswer:
    """Walk Akoma Ntoso XML, namespace-agnostic via local-name() XPath."""
    questions = root.xpath(f"//*[local-name()='question' and @eId=$eid]", eid=e_id)
    if not questions:
        # Sometimes eId is on a child or formatted differently. Try a relaxed match.
        questions = root.xpath(f"//*[local-name()='question' and contains(@eId,$eid)]", eid=e_id)
    q_text = _text(questions[0]) if questions else ""

    # Answer: gather all <speech> blocks. For written PQs the file typically holds one
    # ministerial reply (possibly covering multiple grouped questions).
    speeches = root.xpath("//*[local-name()='speech']")
    answer_chunks: list[str] = []
    minister_name: str | None = None
    for sp in speeches:
        from_elems = sp.xpath("./*[local-name()='from']")
        if from_elems and minister_name is None:
            minister_name = " ".join(s.strip() for s in from_elems[0].itertext()).strip() or None
        body = _text(sp)
        if body:
            answer_chunks.append(body)
    answer_text = "\n\n".join(answer_chunks) if answer_chunks else None
    return QuestionAnswer(
        question_text=q_text,
        answer_text=answer_text,
        minister_name=minister_name,
        is_answered=bool(answer_text),
    )


def resolve_party_and_constituency(member: MemberRecord, on_date: date) -> tuple[str | None, str | None]:
    """Pick the party/constituency entry whose dateRange covers on_date."""
    import json
    def pick(items_json: str, *preferred_keys: str) -> str | None:
        try:
            items = json.loads(items_json or "[]")
        except json.JSONDecodeError:
            return None
        def value_of(it: dict) -> str | None:
            for k in preferred_keys:
                v = it.get(k)
                if v:
                    return v
            return None
        candidate = None
        for it in items:
            start = it.get("start")
            end = it.get("end")
            try:
                s = date.fromisoformat(start) if start else None
                e = date.fromisoformat(end) if end else None
            except ValueError:
                s = e = None
            if (s is None or s <= on_date) and (e is None or on_date <= e):
                return value_of(it)
            candidate = candidate or value_of(it)
        return candidate
    # Prefer human-readable showAs; fall back to partyCode/constituency.
    party = pick(member.parties_json, "showAs", "partyCode")
    constituency = pick(member.constituencies_json, "constituency", "showAs")
    return party, constituency
