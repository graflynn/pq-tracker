# pq-tracker × Cowork — issues & suggested fixes

A running log of issues, workarounds, and suggested fixes encountered while
using the **pq-tracker** plugin (and the surrounding Cowork artifact API)
from Cowork sessions. Hand this file to Code when iterating on the plugin
or its skill; route Cowork-API entries upstream as appropriate.

## How to use this file

Every entry has the same shape so Code can scan it quickly:

- **Date** — when it surfaced
- **Area** — `plugin` / `skill` / `cowork-artifact-api` / `cowork-general` /
  `agent-behaviour`
- **Severity** — `blocker` / `annoyance` / `nice-to-have`
- **Symptom** — what the user (or Claude) actually saw
- **Diagnosis** — root cause where known; mark `unverified` if not
- **Workaround** — what we did in the moment to get unstuck
- **Suggested fix** — concrete change Code should make

Add new entries at the **top** so the most recent is first.

---

## 2026-05-17 — No discovery path makes future sessions read this file

- **Area:** skill (documentation)
- **Severity:** annoyance
- **Symptom:** The file persists at a stable path in the user's project
  folder and documents its own conventions, but nothing makes a future
  Cowork session aware that it exists or that it should be consulted
  before doing pq-tracker work. So entries accumulate, but the next
  session re-encounters the same issues from scratch.
- **Diagnosis:** Two missing links. (1) The pq-research skill doesn't
  reference ISSUES.md, so the skill load doesn't pull the log into
  context. (2) Cowork's auto-memory system — implied by the
  `consolidate-memory` skill — isn't wired up in this session: no
  `MEMORY.md` index, no memory directory, and the "auto-memory section"
  the skill refers to isn't in the visible system prompt for Cowork mode.
- **Workaround:** User has to remember to point each new session at the
  log, or work in the project folder so a `CLAUDE.md` hint file could be
  picked up (also unreliable, since Cowork doesn't auto-mount user
  folders).
- **Suggested fix (Skill — primary):** Add this paragraph to
  `pq-research/SKILL.md`, ideally near the top:
  > Before starting significant pq-tracker work, look for an
  > `ISSUES.md` file in the user's pq-tracker project folder
  > (typically `~/Documents/pq-tracker/`). It logs known plugin /
  > skill / Cowork-API issues, their workarounds, and suggested fixes.
  > Respect the entry format documented at the top of that file when
  > appending new entries.
  >
  > User shortcuts for the log:
  > - "log it" → append a new entry with today's date
  > - "close that one" → mark the matching entry **Resolved: YYYY-MM-DD**
- **Suggested fix (Cowork — secondary):** Wire up the auto-memory
  system in Cowork mode the same way Code has it: a memory directory
  (e.g., `~/.claude/memory/` or per-workspace), a `MEMORY.md` index,
  and a system-prompt section describing both. Without it, the
  `consolidate-memory` skill has nothing to operate on. Once that's
  in place, a per-project memory file pointing at the project's
  `ISSUES.md` would be a belt-and-braces alternative to the skill
  edit.

## 2026-05-17 — Agent stuck on artifact route instead of pivoting to Python

- **Area:** agent-behaviour (skill nudge needed)
- **Severity:** annoyance
- **Symptom:** Asked for "a heatmap of CGM PQs by constituency," Claude
  tried twice to render it inside a live Cowork artifact (HTML + JS calling
  `pq_list` through the bridge), hit the undocumented `callMcpTool` return
  shape, and gave up empty. User had to point out that
  matplotlib/seaborn in the sandbox would have produced a real heatmap in
  one shot.
- **Diagnosis:** The pq-tracker skill nudges toward live, refreshable
  outputs because most uses of the plugin are exploratory. That's a fine
  default *when interactivity is wanted*, but there's no fallback guidance
  for "the artifact route is flaky / static is fine / treat the plugin
  purely as a data source and plot elsewhere." Claude defaulted to the
  hardest path.
- **Workaround:** Switched to Python + matplotlib in the workspace bash
  sandbox to render the heatmap as a PNG; embedded the data fetched
  through the MCP tools directly into the script.
- **Suggested fix (Skill):** Add a short "Choosing the output format"
  section to `pq-research/SKILL.md`:
  > The plugin's tools are the data source. Visualisations can be built
  > with whatever tool fits the deliverable: matplotlib/seaborn for static
  > PNG/SVG charts (most reliable), xlsx for tabular reports with
  > conditional formatting, pptx/docx for embedded charts in documents,
  > and Cowork artifacts only when the user wants interactivity or a
  > re-openable live view. Don't default to artifacts when a static
  > image will do — they have more failure modes (bridge timing,
  > response-shape quirks, CDN restrictions).

## 2026-05-17 — Artifact: `callMcpTool` response shape is undocumented (unresolved)

- **Area:** cowork-artifact-api
- **Severity:** blocker (in current attempt)
- **Symptom:** Calling `mcp__plugin_pq-tracker_pq-tracker__pq_list` from inside a
  Cowork artifact via `window.cowork.callMcpTool(name, args)` returned a value
  whose `items` array could not be located by any of the standard MCP-result
  unwrap shapes (`r.content[].text` JSON, `r.result`, `r.toolResult`, `r.data`,
  or the raw object). The artifact rendered "No CGM-tagged PQs returned."
  even though the same call against the same backend returned 116 items in
  server-side use.
- **Diagnosis:** *Unverified.* The bridge probably wraps results in a shape
  not covered by the unwrap fallbacks we tried. No debug dump in scope yet.
- **Workaround:** Pivoted to Python + matplotlib for the chart (see the
  agent-behaviour entry above). Artifact debug not pursued further.
- **Suggested fix (Cowork):** Document the **exact** return shape of
  `window.cowork.callMcpTool(name, args)` in the `create_artifact` tool
  description, with at least one example response for a connector that
  returns a JSON object. A single example would unblock authoring entirely.

## 2026-05-17 — Artifact: `window.cowork.callMcpTool` not available at script start

- **Area:** cowork-artifact-api
- **Severity:** annoyance
- **Symptom:** An IIFE that does `if (!window.cowork.callMcpTool) { showError }`
  at parse-time fires the error path even though the bridge ultimately
  attaches a few hundred ms later.
- **Diagnosis:** The Cowork → artifact bridge is injected after the artifact
  HTML parses; there's no `cowork:ready` event or `await window.coworkReady`
  contract.
- **Workaround:** Poll for `window.cowork.callMcpTool` (and a couple of
  alternative spellings) every 80 ms for up to ~8 s before giving up.
- **Suggested fix (Cowork):** Either (a) fire a `cowork:ready` event the
  artifact can `await`, (b) expose `window.coworkReady` as a Promise, or
  (c) inject the bridge before any author script executes. Document the
  contract in the `create_artifact` tool description.

## 2026-05-17 — Skill: no recipe for heatmap / 2D-matrix outputs

- **Area:** skill (documentation)
- **Severity:** nice-to-have
- **Symptom:** First attempt at "a heatmap of PQs by constituency" produced
  a 1D shaded list, not a true 2D matrix, because the agent didn't probe
  for the second axis up front.
- **Diagnosis:** `pq-research/SKILL.md`'s "Common report shapes" covers
  *Constituency profile*, *Topic trend*, *"What did HSE actually say"*, and
  *TD activity* — but not heatmap / matrix layouts. The user's mental model
  for "heatmap" is 2D, so the agent should ask for the second axis before
  building anything.
- **Workaround:** Asked the user post-hoc and rebuilt. Logged the friction.
- **Suggested fix (Skill):** Add a **Heatmap / matrix** entry to the
  "Common report shapes" section. Mandatory clarifications to ask up front:
  rows axis (default: constituency), columns axis (year / month / tag /
  TD / party), scope filter (which tag set or FTS clause), normalisation
  (raw count vs per-capita vs share-of-row). Recommended tools:
  `pq_aggregate` per-row for symmetric matrices, `pq_list` +
  client-side grouping for ragged matrices, **matplotlib/seaborn from
  the sandbox for the actual rendering**.

## 2026-05-17 — `pq_aggregate` has no second axis, encouraging `pq_sql` reach

- **Area:** plugin (tool design) + skill
- **Severity:** nice-to-have
- **Symptom:** To produce a constituency × TD matrix Claude reached for
  `pq_sql` to do a `GROUP BY td_constituency, td_name` in one call, which
  the user flagged as the wrong tool for the job ("this shouldn't be a SQL
  query anyhow").
- **Diagnosis:** `pq_aggregate` accepts only one `group_by` axis. The
  alternatives are (a) loop `pq_aggregate(group_by="member", constituency=[X])`
  per constituency (N round-trips) or (b) `pq_list` + client-side grouping.
  Both are fine but neither is documented as the canonical pattern.
- **Workaround:** Switched to `pq_list(tag=['cgm'], limit=500)` + grouping
  in Python.
- **Suggested fix:**
  - **Plugin:** Add a `secondary_group_by` argument to `pq_aggregate` so a
    single call can return e.g. `{ constituency, member, count }` rows.
  - **Skill:** Until then, add an explicit "ragged matrix" recipe noting
    `pq_list` + client-side grouping is preferred over `pq_sql` for this
    shape.

## 2026-05-17 — `pq_list` with `limit=200` exceeds the agent's tool-output cap

- **Area:** plugin (tool design)
- **Severity:** annoyance
- **Symptom:** `pq_list(tag=['cgm'], limit=200)` returned ~94 KB / 2,627
  lines (116 items). The agent harness diverted the result to a file
  rather than putting it in context.
- **Diagnosis:** Each item carries `snippet`, `tags[]`, `party`, `permalink`,
  `minister`, `department`, plus full date fields and status. The `snippet`
  itself isn't all that compact (often 200–400 chars). Multiplied across
  100+ items the response balloons.
- **Workaround:** Used a subagent to parse the saved file and report a
  compact summary, then loaded the same file from Python for the plot.
- **Suggested fix:**
  - Add a `fields=[…]` parameter so the caller can request only the
    columns they need (e.g., `pq_ref,td_constituency,member` for grouping
    operations).
  - Or add a `compact: true` mode that drops `snippet`, `tags`,
    `minister`, `department` from the rows.
  - Either way, tighten the snippet hard-cap (~120 chars) and document it
    in the tool docstring.

## 2026-05-17 — Flask API `start-server.cmd` is an undocumented prerequisite

- **Area:** plugin (architecture)
- **Severity:** annoyance
- **Symptom:** Even after the MCP server starts cleanly and Cowork
  registers the `pq_*` tools, every call fails with a connection error
  until the user separately launches `start-server.cmd` to bring up the
  Flask API at `127.0.0.1:5454`.
- **Diagnosis:** The MCP server is a thin proxy to a Flask API in a
  separate process. Only the proxy starts via `.mcp.json`; the Flask app
  is its own thing.
- **Workaround:** Tell the user to launch `start-server.cmd` manually
  before opening Cowork.
- **Suggested fix:** Pick one of —
  - Have `run-mcp.cmd` spawn the Flask API as a subprocess and tear it
    down on exit.
  - Merge the Flask API into the MCP-server process (FastAPI/uvicorn
    embedded, or just drop the HTTP layer and call into the Python
    module directly).
  - At minimum, return a **helpful, actionable** error from any tool
    call when the API is unreachable: *"pq-tracker Flask API not
    running. Launch `start-server.cmd` in the repo root and retry."*

## 2026-05-17 — `run-mcp.cmd` lands in a directory without `.venv` or `pq_tracker`

- **Area:** plugin (packaging)
- **Severity:** blocker
- **Symptom:** After installing pq-tracker via the marketplace, **no**
  `pq_*` MCP tools appeared in a Cowork session even though the plugin
  showed as installed. `mcp__plugins__list_plugins` listed it; no tools
  were exposed.
- **Diagnosis:** `.mcp.json` spawns `cmd /c "${CLAUDE_PLUGIN_ROOT}\run-mcp.cmd"`.
  That script does `cd /d "%~dp0\.."` then runs
  `.venv\Scripts\python.exe -m pq_tracker.mcp_server`. The `..` works
  when the plugin folder lives **inside** the source repo (dev install),
  because the parent is the repo root with `.venv\` and the `pq_tracker\`
  package. After a marketplace install the plugin lives at
  `…\rpm\plugin_<id>\`; `..` lands in `…\rpm\` next to other unrelated
  plugins — no venv, no `pq_tracker` package. The launcher exits
  immediately and Cowork has nothing to register.
- **Workaround:** Edited `run-mcp.cmd` to use an absolute
  `cd /d <path to source repo>` and restarted Cowork.
- **Suggested fix:** Decouple the launcher from "live next to a source
  repo." Options, in rough order of preference:
  1. Bundle the venv and the `pq_tracker` Python package **inside** the
     plugin folder, and have `run-mcp.cmd` use paths relative to
     `%~dp0` (not `%~dp0\..`).
  2. Ship the MCP server as a stand-alone executable (PyInstaller) so
     there's no Python or venv discovery problem at all.
  3. Read `PQ_TRACKER_REPO_ROOT` from the environment with a clear
     error message if it isn't set, instead of silently failing on
     a relative path.
- **Resolved: 2026-05-17** (commit `2e346e2`). Took option 3 with a
  hardcoded fallback: `run-mcp.cmd` now reads `PQ_TRACKER_HOME` (defaults
  to `C:\Users\Grainne\Documents\pq-tracker`) and emits a loud
  `[run-mcp] ERROR:` to stderr if the venv isn't present at that path,
  instead of silently exiting. Override path-side without editing the
  script by setting `PQ_TRACKER_HOME` in the plugin's `.mcp.json` env
  block. Source pushed to GitHub so future marketplace installs are
  clean; the deployed copy under `…\rpm\plugin_<id>\` was patched in
  parallel so the running Cowork session didn't need a reinstall.
  Options 1 (bundle venv) and 2 (PyInstaller) remain available if the
  plugin ever needs to run on a machine that isn't Grainne's.

---

## Open questions for Code

- What is the exact return shape of `window.cowork.callMcpTool` inside
  an artifact, for a connector tool that returns a JSON object?
- Is there a canonical `pq_list` recipe for "give me every PQ matching
  this filter, compact enough to keep in agent context"? If not, see
  the `fields=`/`compact:` suggestion above.

## Conventions

- Dates in `YYYY-MM-DD` form.
- One entry per distinct issue. If we hit the same thing twice, add a
  second line under **Symptom** noting the recurrence and date.
- Keep entries terse but specific — Code should be able to act on the
  **Suggested fix** without re-reading the conversation.
