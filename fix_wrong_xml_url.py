r"""Fix questions rows whose stored xml_url points at the wrong debate section.

Symptom. A row's ``xml_raw`` carries one or more ``<question eId="pq_N">``
elements, but the row's own eId (derived from ``question_uri``) is not among
them. That means the XML we have is the response document for a *different*
question; consequently ``question_text``, ``answer_text`` and ``answer_id``
are all wrong for this row. Originally diagnosed for PQ 33698/26 — see
``project_pq_tracker.md`` memory entry "eId-mismatch guard".

Strategy. The xml_url looks like ::

    https://data.oireachtas.ie/akn/ie/debateRecord/dail/YYYY-MM-DD/writtens/mul@/dbsect_N.xml

We assume the section number is close to the true one (most cases are off by
< 20). We walk dbsect_(N-k).xml and dbsect_(N+k).xml for k = 1..MAX, fetching
each candidate, parsing it, and checking if our eId is in its ``<question>``
elements. First hit wins.

On a hit we:
  - replace xml_url, xml_raw on the row,
  - re-derive question_text / answer_text / minister_name from the new XML,
  - canonicalize answer via ``db.upsert_answer`` and set ``answer_id``,
  - flip answer_status / date_answered if newly-answered,
  - re-embed via ``embeddings.embed_pq`` (idempotent).

Defaults to dry-run. With ``--apply`` it commits.

    .\.venv\Scripts\python.exe fix_wrong_xml_url.py
    .\.venv\Scripts\python.exe fix_wrong_xml_url.py --apply
    .\.venv\Scripts\python.exe fix_wrong_xml_url.py --apply --max-step 30
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import date as date_cls
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pq_tracker import config as cfg  # noqa: E402
from pq_tracker import db  # noqa: E402
from pq_tracker import embeddings as emb  # noqa: E402
from pq_tracker.oireachtas import OireachtasClient  # noqa: E402

log = logging.getLogger("fix_xml_url")

_DBSECT_RE = re.compile(r"(.*?/)dbsect_(\d+)(\.xml)$")
_EID_RE = re.compile(r'<question\s+[^>]*?eId="([^"]+)"')
DEFAULT_MAX_STEP = 30


def _find_mismatches(conn) -> list[dict]:
    """Return rows whose own eId is not present in their stored xml_raw."""
    rows = conn.execute(
        "SELECT pq_ref, question_uri, xml_url, date_asked, xml_raw, answer_status "
        "FROM questions WHERE xml_raw IS NOT NULL AND xml_raw != ''"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        xml = r["xml_raw"]
        if isinstance(xml, (bytes, bytearray)):
            xml = bytes(xml).decode("utf-8", errors="replace")
        eids = _EID_RE.findall(xml)
        if not eids:
            continue
        my_eid = (r["question_uri"] or "").rsplit("/", 1)[-1]
        if my_eid and my_eid not in eids:
            out.append({
                "pq_ref": r["pq_ref"],
                "question_uri": r["question_uri"],
                "xml_url": r["xml_url"],
                "date_asked": r["date_asked"],
                "my_eid": my_eid,
                "stored_eids": eids,
                "answer_status": r["answer_status"],
            })
    return out


def _candidate_urls(xml_url: str, max_step: int) -> list[tuple[int, str]]:
    """Generate (section_no, url) pairs near the recorded section, alternating ±k."""
    m = _DBSECT_RE.match(xml_url)
    if not m:
        return []
    prefix, n_str, suffix = m.group(1), m.group(2), m.group(3)
    n = int(n_str)
    out: list[tuple[int, str]] = []
    for k in range(1, max_step + 1):
        for delta in (k, -k):
            sec = n + delta
            if sec <= 0:
                continue
            out.append((sec, f"{prefix}dbsect_{sec}{suffix}"))
    return out


def _process_one(client: OireachtasClient, conn, mismatch: dict, *,
                 max_step: int, apply: bool) -> dict:
    pq_ref = mismatch["pq_ref"]
    my_eid = mismatch["my_eid"]
    xml_url = mismatch["xml_url"]
    log.info("[%s] eId=%s stored xml=%s (eIds: %s)", pq_ref, my_eid,
             xml_url.rsplit("/", 1)[-1], ",".join(mismatch["stored_eids"][:6]))

    candidates = _candidate_urls(xml_url, max_step)
    if not candidates:
        return {"pq_ref": pq_ref, "outcome": "no_parseable_section_number"}

    for sec, cand_url in candidates:
        log.info("[%s]   probe dbsect_%d", pq_ref, sec)
        try:
            qa = client.fetch_xml(cand_url, my_eid)
        except Exception as e:  # noqa: BLE001
            log.debug("[%s]     fetch %d failed: %s", pq_ref, sec, e)
            continue
        if qa is None or not qa.xml_raw:
            continue
        xml_text = qa.xml_raw.decode("utf-8", errors="replace")
        eids = _EID_RE.findall(xml_text)
        if my_eid not in eids:
            continue
        log.info("[%s]     HIT dbsect_%d (eIds: %s)", pq_ref, sec, ",".join(eids[:6]))
        if not qa.question_text:
            log.warning("[%s]     hit had no question_text — skipping", pq_ref)
            return {"pq_ref": pq_ref, "outcome": "hit_without_question_text",
                    "section": sec}
        if not apply:
            return {"pq_ref": pq_ref, "outcome": "would_fix",
                    "from_section": int(_DBSECT_RE.match(xml_url).group(2)),
                    "to_section": sec,
                    "is_answered": qa.is_answered}

        new_status = "answered" if qa.is_answered else mismatch["answer_status"]
        answer_id = db.upsert_answer(conn, qa.answer_text)
        # Preserve existing date_answered if already set; only fill it now if
        # the row was pending and we just landed an answer.
        existing = conn.execute(
            "SELECT date_answered FROM questions WHERE pq_ref = ?", (pq_ref,)
        ).fetchone()
        existing_date_answered = existing["date_answered"] if existing else None
        date_answered = existing_date_answered
        if qa.is_answered and not existing_date_answered:
            date_answered = mismatch["date_asked"]
        conn.execute(
            """UPDATE questions
                  SET xml_url       = ?,
                      xml_raw       = ?,
                      question_text = ?,
                      answer_text   = ?,
                      answer_id     = ?,
                      minister_name = COALESCE(?, minister_name),
                      answer_status = ?,
                      date_answered = ?,
                      last_updated_at = ?
                WHERE pq_ref = ?""",
            (cand_url, qa.xml_raw, qa.question_text, qa.answer_text, answer_id,
             qa.minister_name, new_status, date_answered,
             time.strftime("%Y-%m-%dT%H:%M:%S"), pq_ref),
        )
        # Re-embed — embed_pq is idempotent on unchanged chunks.
        try:
            emb.embed_pq(conn, pq_ref, qa.question_text, qa.answer_text)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s]     embed_pq failed: %s", pq_ref, e)
        conn.commit()
        return {"pq_ref": pq_ref, "outcome": "fixed",
                "from_section": int(_DBSECT_RE.match(xml_url).group(2)),
                "to_section": sec,
                "is_answered": qa.is_answered}
    return {"pq_ref": pq_ref, "outcome": "no_match_within_window",
            "tried": len(candidates)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="commit (default is dry-run, only probes downloads)")
    parser.add_argument("--max-step", type=int, default=DEFAULT_MAX_STEP,
                        help="how many sections ± to walk (default: %d)" % DEFAULT_MAX_STEP)
    parser.add_argument("--pq-ref", type=str, default=None,
                        help="limit to a single pq_ref (default: every mismatch)")
    args = parser.parse_args()

    cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = cfg.LOGS_DIR / f"fix-xml-url-{time.strftime('%Y%m%d-%H%M%S')}.log"
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(log_path, encoding="utf-8")],
                        force=True)

    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        mismatches = _find_mismatches(conn)
        if args.pq_ref:
            mismatches = [m for m in mismatches if m["pq_ref"] == args.pq_ref]
        log.info("=== fix start: %d mismatched row(s) (apply=%s) ===",
                 len(mismatches), args.apply)
        log.info("log: %s", log_path)

        client = OireachtasClient(fetch_delay_ms=500)
        outcomes: list[dict] = []
        for m in mismatches:
            try:
                out = _process_one(client, conn, m,
                                   max_step=args.max_step, apply=args.apply)
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] unhandled: %s", m["pq_ref"], e)
                out = {"pq_ref": m["pq_ref"], "outcome": "exception",
                       "error": str(e)}
            outcomes.append(out)

    print("\n=== summary ===")
    by_outcome: dict[str, int] = {}
    for o in outcomes:
        by_outcome[o["outcome"]] = by_outcome.get(o["outcome"], 0) + 1
    for k in sorted(by_outcome):
        print(f"  {k}: {by_outcome[k]}")
    for o in outcomes:
        if o["outcome"] in {"would_fix", "fixed"}:
            print(f"  {o['pq_ref']}: dbsect_{o['from_section']} → dbsect_{o['to_section']}"
                  f"  (answered={o.get('is_answered')})")
    if not args.apply:
        print("\n(dry-run — re-run with --apply to commit)")
    print(f"\nlog: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
