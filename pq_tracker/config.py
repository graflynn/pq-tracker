from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "db"
DB_PATH = DB_DIR / "pqs.sqlite"
EXPORTS_DIR = ROOT / "exports"
XLSX_PATH = EXPORTS_DIR / "pqs.xlsx"
QUESTIONS_DIR = ROOT / "questions"
SUMMARY_PATH = ROOT / "summary.md"
LOGS_DIR = ROOT / "logs"
TOPICS_PATH = ROOT / "topics.yaml"
HSE_PDF_DIR = ROOT / "hse_pdfs"


@dataclass(frozen=True)
class Config:
    # search_terms gate ingestion: a PQ is only stored if at least one term appears
    # in show_as / question_text / answer_text. keywords are tags applied to the
    # ingested rows (display/filtering only — removing one drops the tag, not the row).
    search_terms: list[str]
    keywords: list[str]
    lookback_days: int
    chambers: list[str]
    xml_fetch_delay_ms: int


def load_config() -> Config:
    with TOPICS_PATH.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    keywords = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()]
    search_terms = [str(k).strip() for k in (raw.get("search_terms") or []) if str(k).strip()]
    # Back-compat: pre-split YAMLs only had `keywords`. If `search_terms` is
    # missing, default to `["diabet*"]` so a stale YAML still gates ingestion
    # sensibly. The /settings UI will surface this for explicit edit.
    if not search_terms:
        search_terms = ["diabet*"]
    if not keywords:
        raise ValueError(f"topics.yaml has no keywords. Add some under 'keywords:' in {TOPICS_PATH}")
    return Config(
        search_terms=search_terms,
        keywords=keywords,
        lookback_days=int(raw.get("lookback_days", 1)),
        chambers=[str(c).strip().lower() for c in (raw.get("chambers") or ["dail"])],
        xml_fetch_delay_ms=int(raw.get("xml_fetch_delay_ms", 250)),
    )


def ensure_dirs() -> None:
    for p in (DB_DIR, EXPORTS_DIR, QUESTIONS_DIR, LOGS_DIR):
        p.mkdir(parents=True, exist_ok=True)


_HEADER = (
    "# search_terms: ingestion gate. A PQ is only stored if at least one term\n"
    "#   appears in its text. Supports trailing-`*` wildcard (e.g. diabet* matches\n"
    "#   diabetes/diabetic/diabetics). Case- and hyphen-insensitive.\n"
    "# keywords: auto-tags applied to ingested PQs for display/filtering. Removing\n"
    "#   a keyword drops the tag, not the row. Use the Settings page button\n"
    "#   'Rebuild auto-tags' to retroactively re-apply after edits.\n"
    "# Both are maintained from /settings, but you can hand-edit too.\n"
    "\n"
)


def save_config(new: Config) -> None:
    """Atomically rewrite topics.yaml. Preserves the standard header comment."""
    payload = {
        "search_terms": list(new.search_terms),
        "keywords": list(new.keywords),
        "lookback_days": int(new.lookback_days),
        "chambers": list(new.chambers),
        "xml_fetch_delay_ms": int(new.xml_fetch_delay_ms),
    }
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    tmp = TOPICS_PATH.with_name(TOPICS_PATH.name + ".tmp")
    tmp.write_text(_HEADER + body, encoding="utf-8")
    tmp.replace(TOPICS_PATH)
