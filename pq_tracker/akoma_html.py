"""Convert Akoma Ntoso (Oireachtas debate XML) to a small HTML subset suitable
for embedding in the modal: <p>, <strong>, <em>, <ul>/<li>, <table>, <h3>.

The XML schema we see uses these structural tags inside <question> and <speech>:
  - <p eId="...">    : paragraph (often with inline <b> / <i>)
  - <list> / <item>  : bullet list
  - <table> with <caption>, <tr>, <td>, nested <p>
  - <heading>        : section heading
  - <from>           : speaker label inside <speech> — we already extract this
                       as minister_name, so we drop it from the body render
  - <recordedTime>   : metadata, drop

Designed for live use at modal-render time, so the XML→HTML walk is small,
allocation-light, and tolerant of unknown elements (recurse).
"""
from __future__ import annotations

import html as _html
import re
from typing import Iterable

from lxml import etree


def _local(elem) -> str:
    return etree.QName(elem).localname


def _esc(s: str) -> str:
    return _html.escape(s, quote=False)


def _inline_html(elem) -> str:
    """Render inline content (text + <b>/<i>/<br>) as HTML."""
    parts: list[str] = []
    if elem.text:
        parts.append(_esc(elem.text))
    for child in elem:
        tag = _local(child)
        if tag in ("b", "strong"):
            parts.append(f"<strong>{_inline_html(child)}</strong>")
        elif tag in ("i", "em"):
            parts.append(f"<em>{_inline_html(child)}</em>")
        elif tag == "br":
            parts.append("<br>")
        elif tag in ("recordedTime", "from"):
            pass
        else:
            # Unknown inline element: render its descendant text only.
            parts.append(_inline_html(child))
        if child.tail:
            parts.append(_esc(child.tail))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _table_html(table) -> str:
    caption_html = ""
    caps = table.xpath("./*[local-name()='caption']")
    if caps:
        c = _inline_html(caps[0])
        if c:
            caption_html = f"<caption>{c}</caption>"
    rows_html: list[str] = []
    for tr in table.xpath(".//*[local-name()='tr']"):
        cells: list[str] = []
        for cell in tr:
            ctag = _local(cell)
            if ctag not in ("td", "th"):
                continue
            ps = cell.xpath("./*[local-name()='p']")
            if ps:
                text = " ".join(_inline_html(p) for p in ps).strip()
            else:
                text = _inline_html(cell)
            cells.append(f"<{ctag}>{text}</{ctag}>")
        if cells:
            rows_html.append("<tr>" + "".join(cells) + "</tr>")
    if not rows_html:
        return ""
    return f"<table>{caption_html}{''.join(rows_html)}</table>"


def _element_html(elem) -> str:
    """Render one structural element to HTML."""
    tag = _local(elem)
    if tag == "p":
        inner = _inline_html(elem)
        return f"<p>{inner}</p>" if inner else ""
    if tag == "list":
        items: list[str] = []
        for child in elem:
            if _local(child) == "item":
                items.append(f"<li>{_inline_html(child)}</li>")
        return f"<ul>{''.join(items)}</ul>" if items else ""
    if tag == "table":
        return _table_html(elem)
    if tag == "heading":
        inner = _inline_html(elem)
        return f"<h3>{inner}</h3>" if inner else ""
    if tag == "block":
        return _children_html(elem)
    if tag in ("recordedTime", "from"):
        return ""
    # Unknown structural element: descend and try its children.
    return _children_html(elem)


def _children_html(elem) -> str:
    parts: list[str] = []
    for child in elem:
        out = _element_html(child)
        if out:
            parts.append(out)
    return "".join(parts)


def question_html(question_elem) -> str:
    """Render a <question> element body as HTML."""
    return _children_html(question_elem)


def speech_html(speech_elem) -> str:
    """Render a <speech> element body as HTML, skipping the speaker label
    (<from>) since the API already returns minister_name separately."""
    parts: list[str] = []
    for child in speech_elem:
        tag = _local(child)
        if tag in ("from", "recordedTime"):
            continue
        out = _element_html(child)
        if out:
            parts.append(out)
    return "".join(parts)


def render_question_and_answer(xml_bytes: bytes, e_id: str) -> tuple[str | None, str | None]:
    """Top-level entry: given the raw XML for a debate section and the eId of
    the specific question, return (question_html, answer_html).

    Returns (None, None) on parse failure. Returns (q_html, None) when speeches
    are absent (pending answer). Answer aggregates ALL <speech> bodies in the
    section, matching how grouped questions are answered by one shared reply.
    """
    if not xml_bytes:
        return None, None
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return None, None
    q_html: str | None = None
    qs = root.xpath("//*[local-name()='question' and @eId=$eid]", eid=e_id)
    if not qs:
        # Relaxed match for legacy / off-by-prefix eIds.
        qs = root.xpath("//*[local-name()='question' and contains(@eId,$eid)]", eid=e_id)
    if qs:
        q_html = question_html(qs[0]) or None
    speeches = root.xpath("//*[local-name()='speech']")
    if speeches:
        parts = [s for s in (speech_html(sp) for sp in speeches) if s]
        a_html = "\n".join(parts) or None
    else:
        a_html = None
    return q_html, a_html
