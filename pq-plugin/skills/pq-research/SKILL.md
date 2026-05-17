---
description: Use this skill to research, analyse, and report on Irish health-related parliamentary questions (PQs). Invoke when the user asks about PQs, TDs, ministers, constituency-level health issues, HSE replies, or trends in topics like diabetes, CGM, insulin pumps, paediatric care, waiting times, etc. The PQ tracker MCP tools are the primary data source but combine freely with web search, internal documents (Outlook, SharePoint), and other context.
---

# PQ Research

You are helping with research over a local corpus of Irish health-related parliamentary questions (PQs). PQs are written questions asked by TDs (Deputies / Teachtaí Dála) to the Minister for Health (and adjacent ministers); the answers are recorded in the Oireachtas record and sometimes accompanied by longer HSE supplementary PDF replies.

## What the `pq-tracker` MCP tools give you

Eight tools, all read-only, all wrapping a local Flask API at `http://127.0.0.1:5454/api/v1`. The Flask app must be running (`start-server.cmd`). If a tool call fails with a connection error, tell the user — don't silently fail.

| Tool | When to reach for it |
|---|---|
| `pq_facets` | First call when the user names a constituency / TD / minister / tag and you don't know the exact spelling. Returns every distinct value plus the date range and total. Cheap (<10 ms). |
| `pq_list` | Filter + paginate. FTS5-ranked when `q` is given. Returns compact rows with a snippet — not full text. |
| `pq_get` | One PQ, full body. Use after `pq_list` when you need the actual question and answer text. |
| `pq_aggregate` | Counts grouped by constituency / member / party / minister / department / status / month / year / tag. The workhorse for *any* "report" or "trends" question. |
| `pq_semantic_search` | Paraphrase / synonym / vague-concept search. BGE-small embeddings. Sources: `question`, `answer`, `hse_paragraph`. |
| `hse_list` | Find HSE supplementary PDFs, especially via `pq_ref` to see what's attached to a specific PQ. |
| `hse_get` | Full extracted paragraphs of one PDF. Cite specific pages. |
| `pq_sql` | Read-only SQL escape hatch when no curated tool fits. Schema is in the tool docstring. mode=ro enforced. |

## Approach principles

- **Start with `pq_facets` when the user names anything by name**. Constituencies are stored exactly as Oireachtas writes them (e.g. `"Dún Laoghaire"`, `"Dublin Bay South"`) — never guess; look them up.
- **Lexical (`pq_list q=`) for exact terms**, **semantic (`pq_semantic_search`) for concepts**. "Continuous glucose monitor" — lexical wins. "Cost burden on families" — semantic wins. When the user is vague, try semantic.
- **`pq_aggregate` before listing** when the question is "how many" / "where" / "when" / "who most often" / "trend". Don't fetch 200 PQs to count them — count first, list only what's interesting.
- **`pq_list` returns snippets, not full text**. If the snippets are enough to answer the question, stop. Only call `pq_get` when the agent actually needs the full answer body.
- **HSE PDFs are often the substantive reply** — when a `pq_get` answer is short and refers to "as set out in the attached", check `hse_pdfs` in the response and fetch with `hse_get`.

## Combining with other sources

The PQ corpus is one input, not the whole story. Mix in:

- **Web search** — current news / policy announcements / what changed since the PQ was answered. PQ data can be months out of date; check the live picture.
- **SharePoint / Outlook (if those connectors are present)** — internal documents, meeting notes, advocacy strategy, prior correspondence with HSE. Useful background, never quoted from the PQ corpus.
- **HSE service plan / circulars** — sometimes referenced in PQ answers; the linked PDFs in the corpus may have them, otherwise the web has the canonical version.
- **Patient/advocacy group reports** — for context on why a question was asked (e.g. Diabetes Ireland publishes data the user may want to cross-reference).

When you mix sources, **be explicit about which source each claim comes from**. Don't blend a 2022 PQ answer with 2026 news as if they're the same statement.

## Vocabulary

- **PQ** = parliamentary question.
- **pq_ref** — the canonical identifier (e.g. `33698/26`). Use it as the citation key.
- **TD** = Teachta Dála; the elected member who asked the question.
- **auto-tags** — Grainne's term for the rule-derived tags in `pq_tags` with state `auto`. They come from `topics.yaml` keyword matches.
- **manual tags** = `pq_tags` with state `user_added` — tags Grainne added herself in the UI.
- **answered** vs **pending** — `answer_status`. Pending means the question is asked but the minister hasn't replied yet (typical lag: 1–2 weeks).
- **HSE** = Health Service Executive — the public-sector health body that answers most clinical questions on the minister's behalf.

## Citation pattern

Every claim sourced from the PQ corpus should be traceable. Two equivalent forms:

- **Inline**: "Donegal had 12 type-1 PQs in 2025 [pq_ref [33698/26](https://www.oireachtas.ie/...) and 11 others]"
- **Footnote list**: end with "Sources: 33698/26, 27913/25, 25023/26 — full text via `pq_get`."

When the response is a structured report (constituency profile, trend analysis), include a small "Method" line at the end naming the tools used (e.g. `pq_aggregate group_by=year tag=cgm`). It makes the result reproducible.

## Common report shapes

These are the patterns Grainne typically asks for. Recognise them and structure accordingly.

### Constituency profile

1. `pq_aggregate group_by=tag constituency=[X]` — what topics dominate
2. `pq_aggregate group_by=member constituency=[X]` — which TDs are most active
3. `pq_aggregate group_by=year constituency=[X]` — activity over time
4. `pq_list constituency=[X] limit=10` — recent samples
5. Output: a 1-page profile with sections "Active TDs / Top topics / Trend / Recent PQs."

### Topic trend

1. `pq_aggregate group_by=year tag=[topic]` — overall trajectory
2. `pq_aggregate group_by=party tag=[topic]` — political distribution
3. `pq_aggregate group_by=constituency tag=[topic] limit=10` — geographic spread
4. `pq_semantic_search q=[topic phrasing] limit=5` — newest framing (catch PQs the auto-tag missed)
5. Output: a chart-ready table + 3-5 illustrative pq_refs.

### "What did HSE actually say about X"

1. `pq_semantic_search q=[X] source=answer limit=5` — find the substantive answers
2. For each: `pq_get` for full context
3. `hse_list` filtered to those pq_refs — any supplementary PDFs?
4. `hse_get` on the relevant PDFs for the detailed text
5. Output: synthesised position with pq_ref citations and PDF page references.

### TD activity

1. `pq_aggregate group_by=tag member=[name]` — their topic profile
2. `pq_list member=[name] limit=20` — recent questions
3. `pq_aggregate group_by=year member=[name]` — how active over time
4. Output: a brief on what this TD has been raising and how often.

## When to use `pq_sql`

The escape hatch. Examples of legitimate uses:

- Cross-table joins the curated tools don't expose (e.g. "find PQs by TDs whose constituency name contains 'Dublin' AND who switched party").
- One-off statistics ("median days from date_asked to date_answered, broken down by minister").
- Schema exploration (`SELECT name FROM sqlite_master WHERE type='table'`).

If you find yourself reaching for SQL frequently for the same shape, that's a hint that a curated tool is missing — call it out to Grainne so she can add one.

Always parameterise (`params=[...]`) rather than string-interpolating values.

## Things to avoid

- **Don't trust auto-tags as ground truth.** They're keyword matches from `topics.yaml`; PQs about a topic can lack the tag if the question phrased things differently. Semantic search backs auto-tags up.
- **Don't quote question_text verbatim without trimming the boilerplate prefix.** Every PQ starts with `"{NUM}. Deputy {Name} asked the Minister for {Title}"` — that's redundant context, the actual question follows. The API endpoints don't strip this; you should.
- **Don't fabricate pq_refs.** If the corpus doesn't have it, say so.
- **Don't claim recency** ("as of today the HSE position is X"). PQ answers reflect the moment they were given. Check date_answered.
