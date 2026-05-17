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
