from __future__ import annotations

import re
from functools import lru_cache


def split_alias(entry: str) -> tuple[str, str]:
    """Parse a keyword entry, optionally of the form ``match:label``.

    Returns ``(match, label)``. With no colon, label == match (legacy
    behaviour). Both sides trimmed; an empty label falls back to the match
    text so a malformed ``foo:`` doesn't lose the entry.

    Examples:
        ``cgm``                              → ('cgm', 'cgm')
        ``continuous glucose monitor*:cgm``  → ('continuous glucose monitor*', 'cgm')
        ``type 1``                           → ('type 1', 'type 1')
    """
    s = (entry or "").strip()
    if ":" not in s:
        return s, s
    match, _, label = s.partition(":")
    match = match.strip()
    label = label.strip() or match
    return match, label


@lru_cache(maxsize=256)
def _compile(keyword: str) -> re.Pattern[str]:
    # Whitespace/hyphen/underscore between tokens is fuzzy; tokens themselves are literal.
    # Trailing `*` on the last token = open-ended suffix (e.g. `diabet*` → `diabet[A-Za-z0-9]*`),
    # so `diabetes` / `diabetic` / `diabetics` all match a single search term. Only honoured at
    # the very end — mid-word `*` is rare in user input and would just confuse the regex.
    raw_tokens = [t for t in re.split(r"[\s\-_]+", keyword.strip()) if t]
    if not raw_tokens:
        return re.compile(r"(?!x)x")
    suffix_wild = raw_tokens[-1].endswith("*") and len(raw_tokens[-1]) > 1
    if suffix_wild:
        raw_tokens[-1] = raw_tokens[-1][:-1]
    tokens = [re.escape(t) for t in raw_tokens]
    body = r"[\s\-_]+".join(tokens)
    left = r"(?<![A-Za-z0-9])"
    right = r"[A-Za-z0-9]*(?![A-Za-z0-9])" if suffix_wild else r"(?![A-Za-z0-9])"
    return re.compile(left + body + right, re.IGNORECASE)


def match_keyword_tags(text: str, keywords: list[str]) -> list[str]:
    """Return tag labels that match the text, case- and hyphen-insensitive.

    Used for auto-tagging the stored corpus from the ``keywords`` list. Each
    entry may be ``match`` (label = match) or ``match:label`` (label decoupled
    from the matched pattern). Output is deduplicated so multiple aliases
    pointing at the same label collapse to one tag.
    """
    if not text:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        match, label = split_alias(kw)
        if not match:
            continue
        if _compile(match).search(text):
            if label not in seen:
                seen.add(label)
                hits.append(label)
    return hits


def match_search_terms(text: str, terms: list[str]) -> bool:
    """True if any search term matches the text. Used as the ingestion gate
    and by ``prune_corpus.py``. Search terms don't tag — they only decide
    whether a PQ enters the corpus — so this returns a bool, not labels.
    Any ``:label`` suffix on an entry is silently ignored (tolerant of paste
    from the keywords list, but not advertised).
    """
    if not text:
        return False
    for entry in terms:
        match, _ = split_alias(entry)
        if not match:
            continue
        if _compile(match).search(text):
            return True
    return False


# A PQ answer "defers to the HSE" when the Minister refers it to the HSE for a
# direct reply rather than answering in the chamber. Those PQs are the ones
# that get a separate HSE supplementary PDF published on about.hse.ie — so this
# predicate is a cheap classifier for "this PQ likely has (or will get) an HSE
# answer PDF". Validated at ~96% recall against PQs we already have HSE PDFs
# linked for (844/874). See hse_cli `backfill-missing`.
_HSE_MENTION_RE = re.compile(r"health service executive|\bhse\b", re.IGNORECASE)
_HSE_DIRECT_RE = re.compile(
    r"direct reply"
    r"|reply directly"
    r"|respond[^.]{0,40}directly"
    r"|directly to the deputy"
    r"|refer[a-z]*[^.]{0,80}\bhse\b[^.]{0,80}direct",
    re.IGNORECASE,
)


def answer_defers_to_hse(answer_text: str | None) -> bool:
    """Heuristic: does this answer hand the PQ off to the HSE for a direct reply?

    Looks for an HSE mention alongside 'direct reply' / 'reply directly' /
    'respond … directly' phrasing — e.g. "the HSE has been asked to reply
    directly to the Deputy" or "referred to the HSE … for direct reply". These
    are exactly the PQs that receive a supplementary HSE PDF answer, so callers
    use this to decide which refs are worth a targeted about.hse.ie lookup.
    """
    if not answer_text:
        return False
    return bool(_HSE_MENTION_RE.search(answer_text) and _HSE_DIRECT_RE.search(answer_text))
