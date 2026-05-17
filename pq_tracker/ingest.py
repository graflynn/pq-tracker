from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config as cfg
from . import db
from . import embeddings as emb
from . import exports
from .matching import match_keyword_tags, match_search_terms
from .oireachtas import (
    OireachtasClient,
    QuestionIndexEntry,
    resolve_party_and_constituency,
)

log = logging.getLogger(__name__)


def _permalink(entry: QuestionIndexEntry) -> str:
    # Public-facing debate URL on oireachtas.ie. We anchor to the eId so the page
    # scrolls to this specific question. The site does NOT accept the "pq_"
    # prefix on the eId in the URL — strip it (probed empirically 2026-05-14:
    # 0/34 sampled rows worked with /pq_NNN/, 34/34 worked with /NNN/).
    eid = entry.e_id
    if eid.startswith("pq_"):
        eid = eid[3:]
    return (
        f"https://www.oireachtas.ie/en/debates/question/{entry.date.isoformat()}/"
        f"{eid}/"
    )


def _populate_members_cache(client: OireachtasClient, conn, chamber: str, house_no: int) -> None:
    """Bulk-fetch all members of a given house and upsert to local cache."""
    members = client.fetch_house_members(chamber, house_no)
    for rec in members:
        db.upsert_member(conn, rec.member_code, rec.full_name, rec.parties_json, rec.constituencies_json)
    log.info("populated members cache: %d records for %s house %s", len(members), chamber, house_no)


def _resolve_member(conn, member_code: str, on_date: date) -> tuple[str | None, str | None]:
    if not member_code:
        return None, None
    cached = db.get_cached_member(conn, member_code)
    if cached is None:
        return None, None
    from .oireachtas import MemberRecord
    rec = MemberRecord(
        member_code=cached["member_code"],
        full_name=cached["full_name"],
        parties_json=cached["parties_json"],
        constituencies_json=cached["constituencies_json"],
    )
    return resolve_party_and_constituency(rec, on_date)


def _poll_pending(client: OireachtasClient, conn) -> tuple[int, int]:
    """Re-fetch XML for every PQ currently in 'pending' status.

    Daily ingest uses a 1-day lookback for the bulk scan to keep API traffic
    low; this pass independently checks whether any pending PQ has since
    received an answer. Narrow UPDATE — touches only the fields that change on
    pending→answered, leaving manual fields and tags untouched.

    Returns (newly_answered_count, errors_count).
    """
    rows = conn.execute(
        "SELECT pq_ref, question_uri, xml_url, raw_question_showas "
        "FROM questions WHERE answer_status = 'pending' AND xml_url != ''"
    ).fetchall()
    if not rows:
        return 0, 0
    log.info("re-polling %d pending PQ(s) for late answers", len(rows))
    newly_answered = errors = 0
    today_iso = date.today().isoformat()
    now = datetime.utcnow().isoformat(timespec="seconds")
    for row in rows:
        e_id = (row["question_uri"] or "").rsplit("/", 1)[-1]
        if not e_id:
            continue
        try:
            qa = client.fetch_xml(row["xml_url"], e_id)
            if qa is None or not qa.is_answered:
                continue
            answer_id = db.upsert_answer(conn, qa.answer_text)
            final_q = qa.question_text or row["raw_question_showas"]
            conn.execute(
                """UPDATE questions
                      SET answer_id       = ?,
                          answer_status   = 'answered',
                          date_answered   = COALESCE(date_answered, ?),
                          minister_name   = COALESCE(?, minister_name),
                          xml_raw         = COALESCE(?, xml_raw),
                          question_text   = COALESCE(?, question_text),
                          last_updated_at = ?
                    WHERE pq_ref = ?""",
                (answer_id, today_iso, qa.minister_name,
                 qa.xml_raw, final_q,
                 now, row["pq_ref"]),
            )
            # Mirror into FTS5 (inline-content: no auto-sync from the base table).
            db.refresh_fts_for_pq(conn, row["pq_ref"], final_q, qa.answer_text)
            # Embed the answer that just landed (and re-embed the question if
            # text changed). embed_pq skips sources whose chunk count matches.
            try:
                emb.embed_pq(conn, row["pq_ref"], final_q, qa.answer_text)
            except Exception as ee:  # noqa: BLE001
                log.warning("embed failed for %s: %s", row["pq_ref"], ee)
            # Refresh the group key — the new xml_raw might change membership.
            db.update_group_key_for_pq(conn, row["pq_ref"])
            conn.commit()
            newly_answered += 1
            log.info("pending re-poll: %s → answered", row["pq_ref"])
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("pending re-poll failed for %s: %s", row["pq_ref"], e)
    log.info("pending re-poll: %d newly answered, %d errors", newly_answered, errors)
    return newly_answered, errors


def run(lookback_days: int | None = None, start_date: date | None = None,
        log_to_file: bool = True) -> dict:
    cfg.ensure_dirs()
    settings = cfg.load_config()
    lookback = lookback_days if lookback_days is not None else settings.lookback_days

    started = datetime.now(timezone.utc)
    log_path = _configure_logging(log_to_file, started)
    log.info("=== pq-tracker run starting at %s ===", started.isoformat(timespec="seconds"))

    today = date.today()
    if start_date is not None:
        date_start = start_date
        date_end = today
        log.info("window: %s .. %s (explicit start-date; covers %d days)",
                 date_start, date_end, (date_end - date_start).days)
    else:
        date_start = today - timedelta(days=lookback)
        date_end = today
        log.info("window: %s .. %s (lookback %d days)", date_start, date_end, lookback)
    log.info("search_terms (gate): %s", settings.search_terms)
    log.info("keywords (tags): %s", settings.keywords)

    client = OireachtasClient(fetch_delay_ms=settings.xml_fetch_delay_ms)
    new_count = 0
    answered_count = 0
    errors = 0
    skipped_already_answered = 0

    with db.connect(cfg.DB_PATH) as conn:
        db.init_schema(conn)
        run_id = db.start_run(
            conn, date_start, date_end,
            {"search_terms": settings.search_terms, "keywords": settings.keywords},
        )
        run_started_iso = datetime.utcnow().isoformat(timespec="seconds")

        for chamber in settings.chambers:
            log.info("--- chamber: %s ---", chamber)
            house_no = client.peek_house_no(chamber, date_start, date_end, qtype="written")
            if house_no is not None:
                _populate_members_cache(client, conn, chamber, house_no)
            else:
                log.info("no questions in window; skipping members cache for %s", chamber)
            scanned = 0
            matched = 0
            for entry in client.iter_questions(date_start, date_end, chamber=chamber, qtype="written"):
                scanned += 1
                # Gate (cheap, no XML fetch yet): does show_as mention any search term?
                # If not, skip — we don't want to ingest this PQ at all.
                if not match_search_terms(entry.show_as, settings.search_terms):
                    continue
                matched += 1
                if not entry.pq_ref:
                    log.warning("no PQ ref in showAs for %s; skipping", entry.question_uri)
                    continue
                # Resumability: a published answer never changes, so once we've stored an
                # answered row we never need to refetch the XML for it again.
                prior_status = db.question_status(conn, entry.pq_ref)
                if prior_status == "answered":
                    skipped_already_answered += 1
                    continue
                try:
                    qa = None
                    if entry.xml_url:
                        qa = client.fetch_xml(entry.xml_url, entry.e_id)
                    if qa is None:
                        # Fall back to the JSON showAs when XML is unavailable.
                        from .oireachtas import QuestionAnswer
                        qa = QuestionAnswer(
                            question_text=entry.show_as,
                            answer_text=None,
                            minister_name=None,
                            is_answered=False,
                        )
                    party, constituency = _resolve_member(conn, entry.member_code, entry.date)
                    answer_status = "answered" if qa.is_answered else "pending"
                    date_answered = entry.date if qa.is_answered else None
                    final_q = qa.question_text or entry.show_as
                    # Auto-tags are independent from the gate: scan the final
                    # stored text against the keywords list. Same logic as
                    # api_topics_rebuild so retroactive rebuilds agree.
                    tag_hits = match_keyword_tags(
                        f"{entry.show_as}\n{final_q}", settings.keywords
                    )
                    result = db.upsert_question(
                        conn,
                        pq_ref=entry.pq_ref,
                        question_uri=entry.question_uri,
                        date_asked=entry.date,
                        date_answered=date_answered,
                        td_name=entry.member_name,
                        td_member_code=entry.member_code,
                        td_party=party,
                        td_constituency=constituency,
                        department=entry.department,
                        minister_name=qa.minister_name,
                        question_text=final_q,
                        answer_text=qa.answer_text,
                        answer_status=answer_status,
                        matched_topics=tag_hits,
                        oireachtas_permalink=_permalink(entry),
                        xml_url=entry.xml_url or "",
                        pdf_url=entry.pdf_url,
                        raw_question_showas=entry.show_as,
                        xml_raw=qa.xml_raw,
                    )
                    if result["inserted"]:
                        new_count += 1
                    if result["newly_answered"] or (result["inserted"] and answer_status == "answered"):
                        answered_count += 1
                    # Embed inline so freshly-ingested rows are searchable in
                    # the same txn. embed_pq is idempotent — when chunk counts
                    # match existing rows it's a no-op (no model load), so this
                    # is free on the happy path of "row unchanged".
                    try:
                        emb.embed_pq(conn, entry.pq_ref, final_q, qa.answer_text)
                    except Exception as ee:  # noqa: BLE001
                        log.warning("embed failed for %s: %s", entry.pq_ref, ee)
                    # Persist group key for the new/updated row. Note: when this
                    # row joins an existing group, the OTHER members' keys are
                    # already correct (they computed the same key from their own
                    # xml_raw — keys are independent of which row gets ingested
                    # first), so no fan-out update is needed.
                    db.update_group_key_for_pq(conn, entry.pq_ref)
                    # Commit per-match so the write lock isn't held across the next
                    # network roundtrip — keeps the UI responsive while a long backfill
                    # runs in parallel. WAL makes per-row commits cheap.
                    conn.commit()
                    if (new_count + answered_count) and (new_count + answered_count) % 50 == 0:
                        log.info("checkpoint: new=%d newly_answered=%d skipped=%d",
                                 new_count, answered_count, skipped_already_answered)
                except Exception as e:
                    errors += 1
                    log.exception("error handling PQ %s: %s", entry.pq_ref or entry.question_uri, e)
            log.info("chamber %s: scanned=%d matched=%d skipped_already_answered=%d",
                     chamber, scanned, matched, skipped_already_answered)

        # Re-poll any PQ still 'pending' regardless of date. With the default
        # 1-day lookback, this is how we still catch answers to old pending PQs
        # without re-scanning the whole 90-day window. Narrow UPDATE preserves
        # manual fields (notes / constituent / hse_pdf_url / tags) automatically.
        late_answers, poll_errors = _poll_pending(client, conn)
        answered_count += late_answers
        errors += poll_errors

        db.finish_run(conn, run_id, new_questions=new_count, newly_answered=answered_count, errors_count=errors)

        # Exports (xlsx + per-question md + summary.md), inside the same txn snapshot.
        log.info("writing exports...")
        exports.write_all(conn, run_started_iso, new_count, answered_count, errors, settings.keywords,
                          date_start, date_end)

    log.info("=== done: new=%d newly_answered=%d errors=%d ===", new_count, answered_count, errors)
    return {
        "new_questions": new_count,
        "newly_answered": answered_count,
        "errors": errors,
        "log_path": str(log_path) if log_path else None,
    }


def _configure_logging(to_file: bool, started: datetime) -> Path | None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_path: Path | None = None
    if to_file:
        cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = cfg.LOGS_DIR / f"run-{started.strftime('%Y%m%d-%H%M%S')}.log"
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path
