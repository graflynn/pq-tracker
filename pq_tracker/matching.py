from __future__ import annotations

import re
from functools import lru_cache


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


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """Return the subset of keywords that appear in text. Case- and hyphen-insensitive."""
    if not text:
        return []
    hits: list[str] = []
    for kw in keywords:
        if _compile(kw).search(text):
            hits.append(kw)
    return hits
