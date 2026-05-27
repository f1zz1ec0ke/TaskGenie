# TaskGenie

Task in, working software (or research, or report) out.

TaskGenie is a 100%-local hierarchical multi-agent system. A top-level CEO
hires managers, who hire workers (or sub-managers), who do the actual
work. Every "agent" is the same `Agent` class — what differs is the
tailored role prompt the parent writes when hiring them.

```
CEO/working                                                          ← decides task shape
├── ⠧ EngineManager       manager/working  [3↻ 1✓ workers]            ← department head
│   ├── ⠧ PhysicsCoreWorker   implementing collision detection         ← spotlight (with narration)
│   ├── ⠧ RenderingWorker     write_workspace
│   ├── ⠧ InputHandlerWorker  exec_code
│   └── ● EngineReviewer       done
├── ⠧ LevelDesignManager  manager/working  [2↻ 1! workers]            ← `!` = blocked/asking worker
│   └── ...
└── ● DocsManager        manager/done    (2 done)                    ← fully done branch collapses
```

The CEO and each manager run their own teams concurrently. A bundled live
TUI shows the whole tree, what every working agent is doing right now,
LLM/token counters, and a chronological event stream.

## What's in the box

- **Recursive Agent primitive.** CEO, manager, and worker are the same
  class with different prompts and tool sets. The whole hierarchy
  collapses to one loop that calls itself.
- **Multi-department CEO.** For multi-facet tasks ("build a game" → engine
  + assets + polish + docs) the CEO hires several department managers
  from the start and runs them in parallel. For single-chunk tasks one
  manager is enough — the prompt teaches the call.
- **Required Reviewer pattern.** Every team that produces a synthesis
  hires a dedicated Reviewer worker whose job is to honestly critique
  the team's output. Manager iterates until Reviewer signs off, then
  completes. Applies recursively.
- **Shared deliverable workspace.** Workers produce real files via
  `write_workspace(path, content)` (create-by-default with explicit
  modes for overwrite/append) and modify them via
  `edit_workspace(path, old_text, new_text)` — surgical edits with
  optimistic-concurrency conflict detection. Files land in a friendly
  `./TaskGenie/<project>/` directory in the user's cwd, NOT buried in
  session metadata.
- **Async parallel siblings.** A manager fans out by calling `assign`
  N times, then one `await_results` gathers them. Each level chooses
  its own fan-out.
- **First-class blockers.** Any agent can `report_blocker(reason,
  needs, partial_progress)`. The blocker propagates up; each level can
  resolve locally (rehire, re-assign, change scope) or escalate. CEO
  blockers prompt the user for guidance and the CEO resumes with full
  message history intact.
- **Mid-task user questions.** The CEO can call `ask_user(question,
  options, context)` to clarify ambiguities (output format, scope,
  audience) BEFORE wasting work on the wrong interpretation. Distinct
  from blockers — "I could guess but cheaper to ask now."
- **Bottom-up follow-up suggestions.** Workers and managers can call
  `suggest_follow_up(what, rationale)` mid-run to flag adjacent work
  they noticed. The suggestion rides up with their final report; the
  parent decides to hire a new sibling or relay further upward.
- **Session persistence.** Quit during a blocker, change code (add a
  tool, fix a bug, edit a prompt), then `taskgenie --resume <id>` —
  the full agent tree is rehydrated, the CEO is told which tools have
  changed, and work continues. `taskgenie --list` shows all sessions.
- **Live TUI.** Two-screen layout with a persistent header strip
  (agent counts by status, LLM calls, tokens up/down, tool calls,
  elapsed). **Screen 1** splits the focus pane (the most-recent agent's
  full per-turn activity log — narrations, tool calls, hires,
  reviews — accumulating as it works, plus a "thinking Ns…" liveness
  timer when the LLM is mid-call) and the hierarchy (every manager,
  every worker, with the spotlight worker's narration highlighted).
  **Screen 2** gives the scrolling event log the whole canvas.
  Press `Tab` to swap screens, or `1` / `2` to pin one. Keybindings
  fall back to no-ops when stdout isn't a terminal.
- **MCP client built in.** Drop a standard `mcp.json` (Claude Desktop /
  Cursor / LM Studio format) at the project root and TaskGenie spawns
  each server as a subprocess and surfaces its tools to workers
  automatically.
- **`exec_code` + `exec_workspace`.** Subprocess with cwd-scoped
  scratch dir or workspace dir respectively, scrubbed env, configurable
  timeout. Workers use `exec_code` for intermediate computation
  (private), `exec_workspace` to actually run/test deliverables they
  produced (e.g. `python game.py`). `$TASKGENIE_WORKSPACE` /
  `$TASKGENIE_SCRATCH` are exposed in both so cross-dir snippets work.
  Not a security sandbox — Docker isolation will land as an opt-in
  backend.
- **Context-window management.** Each agent runs an "Archivist" pass
  before any LLM call that would cross
  `TASKGENIE_COMPACT_AT_PCT * TASKGENIE_CTX_WINDOW`. The Archivist is
  a one-shot LLM summarisation that compresses the middle band of the
  agent's history into a single `[archived summary]` user message,
  preserving the original task and the last N turns verbatim.
  Out-of-band tool outputs over `TASKGENIE_DUMP_OVER_BYTES` are
  written to scratch and replaced inline with a one-line pointer the
  agent can `read_file` if needed. Together these keep long-running
  workers (the new `max_turns=1000` default) from blowing the model's
  context budget.
  When the child completes, the parent's `await_results` reply
  carries a `compaction` field (count / skipped / failed /
  tokens_saved / duration_ms — zero fields dropped) so the manager
  can see "this child needed 3 compactions" and split the task
  across siblings on the next pass.
- **Artifacts every run.** On completion, the CEO's final report is
  written verbatim to `final.md`, rendered to a self-contained
  `final.html`, and bundled with stats + agent tree into `final.json`
  inside the session dir. A `README.md` copy lands in the project dir
  so `cd ./TaskGenie/MyProject && cat README.md` shows the report
  right next to the deliverables. `-o PATH` writes an extra copy to a
  user-chosen path with format inferred from the extension.
- **Event log + run summary.** Every event streams to
  `.taskgenie/sessions/<id>/events.jsonl`. After each run a
  deterministic stats panel reports agent counts by role, blockers
  (resolved internally vs escalated), reviews accepted/rejected,
  user questions, follow-ups raised, total LLM calls, total token
  usage, tool calls, wall time — complemented by the CEO's
  qualitative "Process retrospective" section in the final report.

## Requirements

- Python 3.11+
- An OpenAI-compatible local endpoint (LM Studio, vLLM, llama.cpp
  server, etc.) with a tool-calling-capable model loaded.
- (optional) `npx` / `uvx` available on PATH if you want the example
  MCP servers (Brave search via Node, Fetch via Python).

## Install

```bash
pip install -e .
cp .env.example .env             # then edit your endpoint and keys
cp mcp.example.json mcp.json     # then edit BRAVE_API_KEY (or remove servers)
```

## Run

```bash
# new task -- deliverables land at ./TaskGenie/build-a-physics-game/
taskgenie "Build a physics game with slides and ball pits."

# pin the project directory name
taskgenie "Build a chess engine" --project ChessEngine

# also write an extra copy of the report somewhere convenient
taskgenie "Compare cloud GPU pricing" -o ~/Documents/gpu-pricing.html

# list sessions
taskgenie --list

# resume a session (or omit the id for the latest)
taskgenie --resume 2026-05-25-build-a-physics-game-e9bc
taskgenie --resume
```

While the run is in progress: the live TUI shows progress, agents
hiring sub-agents in real time, and any mid-task questions the CEO
asks. Ctrl+C at any time saves state — `--resume` picks up exactly
where you left off.

### Interactive follow-ups

When the CEO completes, you stay in the session and can give
follow-up tasks. The CEO's team and the workspace files persist
between turns:

```
> Anything else? (/done /status /retry /reset, or a follow-up task)
you > make the level harder, double the ball pit count
```

The CEO sees the workspace, decides whether to re-assign existing
managers (they remember what they built) or hire new ones, and
returns when the follow-up is done. Artifacts (`final.md`,
`README.md` in the project dir) regenerate after every turn so the
latest state is always on disk.

Slash commands:

| command | what it does |
|---|---|
| `/done` | end the session cleanly. `--resume` can revive it later. |
| `/status` | print the current agent tree + stats without giving a new task. |
| `/retry` | replay your previous follow-up unchanged (e.g. if the team got something wrong). |
| `/reset` | fire every subordinate agent (workspace files persist). Treat the next task as a fresh start with the existing workspace as context. |

## Where files land

After a run named `Build a physics game`:

```
./TaskGenie/build-a-physics-game/      ← USER-FACING: open this folder
  game.py                              ← worker-produced deliverables
  entities.py
  index.html
  README.md                            ← copy of the CEO's report

./.taskgenie/sessions/<auto-id>/       ← INTERNAL: session metadata
  state.json                           ← paused-state for --resume
  events.jsonl                         ← every event for forensics
  final.md, final.html, final.json     ← formal artifacts
  scratch/<agent_id>/...               ← per-agent private notes
```

Set `TASKGENIE_PROJECTS_DIR` to change the `./TaskGenie/` root.

## Configuration (`.env`)

| var | default | meaning |
|---|---|---|
| `TASKGENIE_BASE_URL` | `http://localhost:1234/v1` | OpenAI-compatible endpoint |
| `TASKGENIE_API_KEY`  | `lm-studio` | Sent as bearer; LM Studio ignores it |
| `TASKGENIE_MODEL`    | (auto-detect) | If empty, picks the first model the endpoint reports |
| `TASKGENIE_TEMPERATURE` | `0.1` | LLM sampling temperature |
| `TASKGENIE_MAX_DEPTH` | `8` | Maximum hierarchy depth (CEO is depth 0) |
| `TASKGENIE_MAX_TURNS` | `1000` | Maximum LLM turns per agent |
| `TASKGENIE_MAX_CONCURRENT_LLM` | `2` | Concurrent in-flight LLM calls cap (Archivist calls bypass) |
| `TASKGENIE_EXEC_TIMEOUT` | `60` | `exec_code` wall-clock timeout (seconds) |
| `TASKGENIE_EXEC_ENV_PASSTHROUGH` | (none) | Extra env vars to expose to `exec_code` |
| `TASKGENIE_MCP_CALL_TIMEOUT` | `60` | Per-call timeout for MCP tool dispatches |
| `TASKGENIE_CTX_WINDOW` | `128000` | Model context-window budget (tokens) the Archivist plans against |
| `TASKGENIE_COMPACT_AT_PCT` | `0.6` | Fraction of `CTX_WINDOW` that triggers a pre-call compaction |
| `TASKGENIE_COMPACT_KEEP_RECENT` | `3` | Most-recent messages preserved verbatim through a compaction |
| `TASKGENIE_DUMP_OVER_BYTES` | `16384` | Tool replies larger than this are written to scratch; the in-memory message becomes a one-line pointer |
| `TASKGENIE_DATA_DIR` | `.taskgenie` | Sessions, scratch, and event logs root |
| `TASKGENIE_PROJECTS_DIR` | `TaskGenie` | Project (deliverables) directory root |
| `TASKGENIE_MCP_CONFIG` | `./mcp.json` | Path to MCP server config |

## MCP config (`mcp.json`)

Standard Claude Desktop / Cursor / LM Studio format:

```json
{
  "mcpServers": {
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": {"BRAVE_API_KEY": "..."}
    },
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"]
    }
  }
}
```

Tools surface to workers with their MCP names (e.g. `brave_web_search`,
`fetch`). Name collisions between servers are auto-namespaced as
`<server>__<tool>`.

## The agents' worldview

**CEO** delegates only. Identifies task shape (research / software /
hybrid). Hires one or many department managers depending on whether
the task has independent facets. For software tasks, the CEO's final
report is a short pointer doc ("see `./TaskGenie/MyProject/game.py`,
run with `python game.py`"), not an inlined code dump — the workspace
is the deliverable.

**Manager** decomposes a sub-task, decides which sub-tasks need their
own sub-manager (multi-faceted) vs a worker (single concrete action),
ALWAYS hires a Reviewer for synthesis work, runs `assign` then
`await_results` for parallelism, and is honest with their parent
about partial team failures rather than tidying them.

**Worker** does the actual work. Has private scratch space for
intermediate notes, shared workspace for deliverable files, and the
MCP-provided tools their session has configured. Their first
obligation is honesty — tool failures get named in the report;
fabrication is sabotage. Workers don't hire others.

All three roles can `report_blocker` if stuck. The CEO additionally
has `ask_user` for mid-task clarifications. Any agent can
`suggest_follow_up` to flag adjacent work for their parent.

## Layout

```
taskgenie/
  agent.py          # the recursive Agent primitive + tool dispatcher
  llm.py            # async OpenAI-compat client (retry, semaphore, token capture)
  events.py         # in-process event bus (subscribe/unsubscribe)
  prompts.py        # CEO / manager / worker role-prompt templates
  config.py         # .env-driven config
  session.py        # save / load / list / tool-diff persistence
  event_logger.py   # background JSONL writer for events (bounded line size)
  stats.py          # deterministic run-summary computation + panel
  mcp_client.py     # spawn MCP server subprocesses; register tools
  artifacts.py      # final.{md,html,json} + project README writer
  context.py        # token counting + large-tool-result dumping to scratch
  archivist.py      # LLM-driven message-history compaction (no bus events)
  tui.py            # Rich live UI: two screens (focus+tree / event log) + header
  result.py         # RunResult kinds: done/blocked/question/exhausted/error
  tools/
    registry.py     # central tool registration + dispatch
    management.py   # schemas for hire/assign/await/review/ask_user/...
    work.py         # schemas + dispatchers for read/write/edit/exec
  __main__.py       # CLI (new / --resume / --list / --output / --project)

tests/
  test_loop.py              # happy path + full escalation chain
  test_persistence.py       # save/load/resume + tool-diff
  test_follow_ups.py        # bottom-up follow_up propagation
  test_followup_loop.py     # CEO follow-up turns after `complete`
  test_interactivity.py     # ask_user round-trip + artifact extension inference
  test_workspace.py         # workspace tools, modes, edits, UTF-8, JSON safety
  test_tui.py               # activity log, key handling, off-tty no-op
  test_review_pra.py        # tool-call sealing, symlink rejection, markup escape
  test_review_prb.py        # session load defensive, ceo-bucket diff, exhaustion, JSON args
  test_review_prc.py        # MCP timeout, proc-tree kill, MCP close logging, edit cleanup
  test_review_prd.py        # token counting, dump_large_tool_result, archivist, retry
  test_review_rt2.py        # round-two: pipeline guard, ctx-error matcher, exec_code workspace
  test_review_rt3.py        # round-three: hardlink/parent-symlink, recursive truncation,
                            #              archivist skip-vs-transient, stats replay-aware
  test_compaction_telemetry.py  # compaction_stats surfaced via to_tool_payload + TUI panel
  test_polish_coverage.py   # _tool_review/_rehire/_fire-cancel, bulk_load edges, MCP
                            # name-collision, resume of asking-status, TUI tool_args clip,
                            # _ColumnOverflowTree narrow-width, _run_resume snapshot-after-mark
```

## Status

- The features listed above are implemented, exercised by the test
  files inventoried just above, and reviewed across three rounds of
  multi-agent code review (notes in `REVIEW_NOTES.md`). What's
  **explicitly NOT a goal** for v1:
  - `exec_code` / `exec_workspace` run real subprocesses with a
    timeout and an env scrub but are **not a security sandbox**.
    Docker-backed isolation is an opt-in we'll add later if someone
    runs this in an environment where the agent's network access
    matters.
  - The MCP client uses stdio transport only (no HTTP MCP yet).
  - No sound/APU emulation work, no Docker, no native binary builds.
- **Known limitations covered in `REVIEW_NOTES.md`** (deliberately
  deferred to a focused future PR, not surprises): bus queues are
  unbounded; `task_assignments` cleanup on cancelled child tasks is
  best-effort (`_drain_pending_assignments` sweeps at `run()` exit);
  `_ColumnOverflowTree` may over-truncate very long agent labels at
  very narrow terminal widths.
- Per-role LLM temperatures are not yet supported — use whatever your
  model lab recommends as a single value.
- The tree pane caps at 10 workers per manager + 10 sub-managers per
  parent with `…+N more` overflow lines; pathological fan-outs beyond
  that are summarised, not hidden.
