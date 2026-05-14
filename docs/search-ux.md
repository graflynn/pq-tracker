# Search UX redesign — design doc

This doc records the agreed approach for redesigning the pq-tracker list-view
search/filter UX. Captured here so it survives session compaction.

## Status — 2026-05-14

**Built:** search modal (triggered by 🔍 on Question header); column-header
filter popovers + sort on every non-aggregate column; multi-select categorical
filters; date-range popovers (with `to` defaulting to today) on both Date asked
and Date answered; text-substring filters on PQ ref / Constituent / Notes;
active-filter pill row with per-pill ✕ and Clear-all; column chooser with
localStorage persistence (key `pq-tracker:visible-columns:v3`); combined TD
column (name + party-abbrev chip + constituency line); horizontal composite
popovers for TD/Party/Constituency and Topic/Tag; default-sort visual cue on
Date header.

**Deltas from original design:**
- Quick toggles (Unanswered-only, My-annotated-only) dropped — replaced by
  the Status column filter popover (`pending` / `answered`) and the existing
  Constituent/Notes filters. Saved the URL shape and CSS room.
- No "required" column concept in the chooser; every column is just default-on
  or default-off and can be toggled freely.
- Composite filter popovers (TD+Party+Constituency, Topic+Tag) chose
  side-by-side columns over stacked sections (smaller vertical footprint,
  fewer scrolls). Each axis has a "clear" link; one shared Apply at bottom.
- Constituent and Notes use text-substring inputs rather than categorical
  popovers (they're free-form fields; distinct values explode otherwise).

**Pending:** result-row redesign — see §7 below. Backend already produces
`snippet(questions_fts, …)` HTML inside `_query_fts`, but `_search_prefilter`
discards everything except the pq_ref + RRF score. The next iteration needs
to thread snippet + per-domain index attribution through to the row render
so we can show a `<mark>`-highlighted excerpt and a `[Lex]` / `[Sem]` /
`[Lex+Sem]` badge.

## Current state (pre-redesign)

The list view has 16 form fields mixing search and filter:

- Free-text (substring `LIKE`) **— removed in current code**
- Question contains (BM25) · Answer contains (BM25) · Semantic
- TD, Party, Constituency, Department, Topic, Tag, Status, Asked-from, Asked-to,
  Constituent annotation

The data grid shows 10 columns: PQ ref, Date, TD, Party, Dept, Topics & tags,
Status, Question excerpt, Constituent, Notes (●).

Backend is in place: porter-stemmed FTS5 (`questions_fts`), BGE-small embeddings
table, `/api/search/bm25` and `/api/search/semantic` endpoints, `_search_prefilter`
helper used by `list_view`.

## Problems

1. Three search boxes ≠ one search. Forces upfront modality choice.
2. No "why did this match?" in result rows — no snippet, no badge.
3. Search and filter conceptually mixed.
4. Grid columns include things Grainne never filters on (constituent, notes).

## Redesign — what we're building

### 1. Top area: search (filtering is separate, see §5)

```
╭──────────────────────────────────────────────╮  ☐ Unanswered only
│ 🔍  Search PQs                                │   [Columns ▾]  [⬇ Excel]
╰──────────────────────────────────────────────╯
  Search in:  ☑ Question   ☑ Answer   ☐ HSE PDFs (Phase 2)
  ▸ Advanced
```

- The three **domain** checkboxes (Question / Answer / HSE) are first-class,
  not hidden behind Advanced. Default: Question + Answer on, HSE off (greyed
  until Phase 2).
- **Advanced** disclosure reveals a flat **index** list, applied to all
  selected domains (NOT a 2D matrix):

  ```
  Indexes:  ☑ Lexical (BM25, stemmed)
            ☑ Semantic (BGE)
  ```

- Default: both indexes on. Untick to restrict.
- Effective search = (selected domains) × (selected indexes). E.g.
  Question+Answer × Lexical+Semantic = the 4 calls we already issue today;
  Question only × Semantic only = a single semantic-on-question call.

### 1a. Filter vs search — the conceptual split

- **Search** is what's in the search bar at the top: it produces a *ranked*
  candidate set scoped to the selected domains/indexes.
- **Filters** are column-header controls (see §5): they *narrow* whatever
  set is currently shown, whether it came from search or from "no search,
  just browse."
- Order of operations: filters first prune the universe → then search
  runs against that pruned set → then results render in score order
  (or date order if no search active).

### 2. PQ-ref handling

Do **NOT** auto-redirect on a typed ref like `21670/26` — multiple documents can
mention a PQ number (HSE PDFs that reference older refs in their body text).

Instead:
- Treat the ref as a normal search term (porter splits `/` so `21670/26` becomes
  `21670` AND `26` and finds anywhere the ref appears).
- If the typed ref exactly matches a row in `questions`, **prepend** that PQ to
  results with a pill: `↗ exact PQ`. Two clicks: row → modal, pill → detail page.

### 3. Search behaviour

- Default treats input as natural language.
- `"quoted phrase"` → BM25 phrase, no semantic.
- Trailing `*` → BM25 prefix wildcard.
- Otherwise: run BM25 and semantic in parallel, RRF-merge, top 200, then apply
  column filters and toggles.

### 4. Grid

**Default columns (5):** PQ ref · Date asked · TD · Topics & tags · Question excerpt

**Optional via [Columns ▾]** (off by default):
- Party · Department · Constituency · Status · Date answered · Constituent ·
  Notes (●) · Score · Match badge

- Column choices persisted to `localStorage` so they survive reloads.
- The [Columns ▾] popover is a list of checkboxes.

### 5. Header behaviour — sort + filter on columns

- **Sort**: clickable on any visible column where sort makes sense (PQ ref, Date
  asked, TD, Status, Date answered). 1st click ASC, 2nd DESC, 3rd clears.
- **Filter**: a small ▾ icon on every filterable column header. Click opens a
  popover anchored to the header. Visual highlight (bold header / coloured dot)
  if a filter is active on that column.
  - **Categorical** (TD, Party, Dept, Constituency, Topic, Tag, Status): popover
    is a searchable list of distinct values with checkboxes (multi-select).
  - **Date asked / Date answered**: popover is a from/to date-range picker
    (two `<input type="date">` fields + Clear button).
- All active column-filters chain with AND. They are independent of the search
  bar — clearing the search box does not clear filters, and vice versa.
- An "Active filters" pill row appears just above the grid when ≥1 filter is
  set, each pill removable with ✕. Includes a "Clear all filters" link.

### 6. Quick toggles (separate from columns / advanced)

- **☐ Unanswered only** — replaces the status dropdown.
- **☐ Only my-annotated** — PQs with a constituent OR notes OR user tag.
  Replaces `has_constituent`, more inclusive.

### 7. Result row design

```
21670/26  ↗exact
2026-03-24  ·  Eamon Scanlon  ·  diabetes (auto)  follow-up      [Lex+Sem]
"…multidisciplinary diabetes team members (consultant endocrinologists,
advanced nurse practitioners)…"
```

- Match badge `[Lex]` / `[Sem]` / `[Lex+Sem]` after chips.
- Score shown on hover, or as an optional column for power-users.
- BM25 snippets use `<mark>` highlights. Semantic excerpt uses the matched
  chunk's text_excerpt.

### 8. Sort defaults

- **No search active**: date_asked DESC, pq_ref ASC (current behaviour).
- **Search active**: relevance DESC. Show a "Sorted by relevance" badge.
  Clicking a column-header sort overrides → switches to that column.

## Implementation order

Estimated total: ~5h.

1. Single search bar + domain checkboxes (Q/A/HSE) + advanced index list +
   endpoint orchestration: ~1h
2. Grid restructure + column chooser + localStorage persistence: ~1.5h
3. Column-header sort + categorical filter popovers + date-range popover +
   active-filter pill row: ~1.5h
4. Result-row redesign (snippet + badges + chips): ~30min
5. "Unanswered" / "my-annotated" toggles: ~20min

## Out of scope for this redesign

- HSE PDF text extraction (Phase 2 of search) — adds the two HSE cells to the
  advanced matrix.
- Cross-encoder reranker (Phase 3 of search) — applies to the top-K of either
  modality.
- Saved searches / recent queries.
- Faceted-search-style live filter counts.

## Things to confirm with Grainne before building (when we resume)

None — direction confirmed. Three earlier decisions also confirmed:
- PQ-ref auto-redirect: **no** (multi-doc reference case).
- Replace existing "Question (excerpt)" column with marked-snippet version: **yes**.
- Filter UI: `<select>` for categorical, popover for dates/constituent.
- Drop columns: constituent, notes, party, dept, status (status becomes a
  separate "Unanswered only" toggle).
- Column chooser: yes.
- BM25 snippet with `<mark>` highlights: confirmed good.
