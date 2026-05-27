# AGENTS.md

Guidance for AI coding assistants (or humans new to the codebase) working
on TaskGenie. The user-facing intro lives in `README.md`; this file is
about navigating, modifying, and reviewing the source.

## What this project IS

A recursive multi-agent system: one `Agent` class, three role flavours
(CEO / manager / worker) differentiated by their tool set and system
prompt. Managers hire managers and/or workers; workers do the actual
work via concrete tools (read/write/edit/exec on the shared workspace,
plus MCP-provided tools).

Two storage zones per session:

- `.taskgenie/sessions/<id>/` — internal: `state.json`, `events.jsonl`,
  per-agent scratch dirs. Implementation detail; the user doesn't
  usually open these.
- `./TaskGenie/<project>/` — user-facing deliverables. Workers write
  here via `write_workspace`. A `README.md` copy of the CEO's report
  lands alongside.

## Layout

| File | What it owns |
|---|---|
| `taskgenie/agent.py` | `Agent` class. Run loop, tool dispatch, compaction integration, pipeline guard. Most "agent behavior" lives here. |
| `taskgenie/llm.py` | `LMStudioClient`. Retry-with-backoff, concurrency semaphore. Archivist calls bypass the semaphore. |
| `taskgenie/events.py` | In-process `EventBus`. Subscribers get `asyncio.Queue`s; producers `put_nowait`. |
| `taskgenie/result.py` | `RunResult` dataclass + `to_tool_payload()` (the JSON shape parents see via `await_results`). |
| `taskgenie/prompts.py` | System prompts for CEO / manager / worker, plus the archivist's prompt. Load-bearing for behaviour. |
| `taskgenie/context.py` | Token counting + tool-output dumping (large tool replies spill to scratch with an in-message pointer). |
| `taskgenie/archivist.py` | History compaction. Pure async function (no bus emits) raising `CompactionSkipped` / `CompactionTransientFailure` so the agent caller distinguishes outcomes. |
| `taskgenie/session.py` | Save / load / list. Atomic write via tempfile + `os.replace` + dir fsync. |
| `taskgenie/event_logger.py` | Background JSONL writer; bounded per-field truncation; `Queue.join()`-based drain. |
| `taskgenie/stats.py` | Deterministic post-run aggregation over `events.jsonl`. Skips replay events (`data["replay"] is True`). |
| `taskgenie/mcp_client.py` | MCP server subprocess management; per-call `wait_for` timeout. |
| `taskgenie/artifacts.py` | `final.{md,html,json}` writer; project-dir `README.md` copy. |
| `taskgenie/tui.py` | Rich-based live UI. Two screens, column-overflow tree, per-agent activity log, compaction summary line. |
| `taskgenie/__main__.py` | CLI entry. New / resume / list. Follow-up loop. Slash commands. |
| `taskgenie/tools/work.py` | Worker tool schemas + dispatchers (read/write/edit/exec on scratch + workspace). |
| `taskgenie/tools/management.py` | Manager + CEO tool schemas (hire/assign/await/review/rehire/fire/ask_user/complete/...). Dispatched directly by `Agent._execute_tool`. |
| `taskgenie/tools/registry.py` | Tool registration, signature hashing for resume-time diff. |

## Conventions you MUST keep

- **`assistant.tool_calls` ↔ `tool` reply invariant.** Every
  `tool_call_id` in an `assistant` message MUST have a following
  `{"role": "tool", "tool_call_id": ...}` reply before the next
  `assistant` turn, or the API 400s. `_seal_tool_batch` in `agent.py`
  exists to maintain this when the loop returns early on
  `complete`/`report_blocker`/`ask_user`. If you add a new early-return
  path inside the for-loop, seal first.

- **Workspace paths go through `_safe_path` + `_safe_open`.** Never
  `Path.read_text` / `Path.write_text` directly on an
  LLM-supplied path. `_safe_path` validates containment and rejects
  NUL/control chars; `_safe_open` adds `O_NOFOLLOW` + hardlink refusal
  (st_nlink > 1) + parent-component symlink re-verification via
  `/proc/self/fd`. The threat model: an LLM tricked or jailbroken into
  emitting an attacker-controlled path shouldn't be able to escape the
  workspace or scribble on `/etc/passwd` via a hardlink.

- **Atomic JSON writes.** `Session.save` uses `NamedTemporaryFile` +
  `os.replace` + `os.fsync` on the directory. The same pattern in
  `artifacts.py` writes `final.{md,html,json}`. Capture `tmp.name`
  IMMEDIATELY after opening — if you defer until after `json.dump`,
  cleanup on serialization failure won't fire.

- **Bus events tagged `replay=True` are for the live TUI only.**
  `bulk_load` re-emits spawn/state/blocked on resume so the TUI can
  paint the rehydrated tree, but `stats.compute` ignores them so
  counters don't double on every resume. If you add a new event type
  that gets replayed, tag it.

- **Tool descriptions are agent-facing prose.** They show up in the
  LLM's prompt. Be specific about failure modes and the recovery
  action for each. See `_EDIT_WORKSPACE_SCHEMA` for the template:
  numbered failure modes with their corresponding remedy.

## Conventions that aren't enforced but matter

- **`_set_status` always, never `agent.status = ...`.** Direct mutation
  skips the `state` event and the TUI never sees the transition.
  Round-three R11 fixed three sites; new code should follow.

- **Prefer `_result()` over `RunResult(...)` in `Agent.run()`.** It
  attaches the live compaction telemetry snapshot so the parent sees
  it via `await_results`.

- **Don't add a fallback / "for backward compat" branch unless you
  have a real failing version to support.** The codebase actively
  removes these — see the rehydrate paths' `.get("...", default)`
  pattern, which IS a real backward-compat need (old snapshots
  pre-date some fields), vs. invented "what if".

## How to add a new agent tool

1. Schema in `taskgenie/tools/work.py` (worker tool) or
   `tools/management.py` (manager/CEO tool). Be specific about
   failure modes.
2. Dispatcher async function: `(agent: Any, args: dict) -> str`.
3. Register in the appropriate `register_*` function with a `kind`:
   `"work"` (workers only), `"management"` (managers + CEO),
   `"both"` (everyone), or `"ceo"` (CEO only).
4. If it touches the filesystem, use `_safe_path` + `_safe_open`.
5. If the result can be large, the framework will dump it to scratch
   automatically via `dump_large_tool_result` -- no special handling
   needed in the dispatcher.

## How to add a new event kind

1. Add the string literal to `taskgenie/events.py:EventKind`.
2. Emit via `self._emit("kind_name", **data)` from the Agent.
3. Handle in `taskgenie/tui.py:_handle_event` if it should surface
   in the live UI.
4. Handle in `taskgenie/stats.py:compute` if it should affect the
   post-run summary.
5. The event will be persisted to `events.jsonl` automatically and
   replayed on resume; tag with `data["replay"] = True` in the
   re-emit path if it shouldn't double-count counters.

## How to add a config knob

1. New `_env_*` line in `taskgenie/config.py:Config`.
2. Document in the README config table (`README.md`).
3. Document in `.env.example`.
4. The value is read once at import time; mutating `CONFIG` mid-test
   requires `object.__setattr__` because the dataclass is frozen.

## Testing conventions

- Every test file is a standalone `python -m tests.test_X` script
  with its own `main()`. No pytest dependency yet. Tests print
  `[name] OK` per case and a summary at the end.
- New behaviour gets a test in either:
  - The matching `tests/test_review_*.py` if it's part of a review
    cluster, or
  - A new `tests/test_<topic>.py` for distinct features.
- Mock LLM `.chat()` methods MUST accept `**_kwargs`. The Archivist
  passes `bypass_semaphore=True`; the real LLM signature accepts it
  but old mocks needed updating in round 2.
- Use `tempfile.mkdtemp()` not `with TemporaryDirectory() as d:` for
  scratch roots that need to outlive the immediate scope -- the
  context-manager form deletes the dir before the test runs (round 2
  tripped on this).
- Mutating `CONFIG.data_dir` in a test: wrap in try/finally to
  restore. Otherwise within-process test pollution.

## How NOT to break the review chain

- Don't make destructive changes outside what was asked. The user
  reviews PRs; surprises in the diff cost their trust budget.
- Use `_set_status` for status changes. Use `_result()` for `RunResult`
  construction in `Agent.run()`. Use `_safe_open` for I/O on paths
  that originated outside the codebase.
- New event kinds get added to `EventKind`. New JSON-emitting writers
  get `default=str`. New tempfiles get a `try/finally` that captures
  the path immediately after open.
- Tests that mutate module globals (`agent_module.compact_messages =
  ...`) MUST restore in `finally`.

## Where to look first when something seems off

- **API 400 on follow-up turn:** dangling `tool_call_id`. Grep for
  the offending id in the message history; trace back to the
  assistant turn that declared it but didn't get a matching tool
  reply. `_seal_tool_batch` is your friend.
- **"Task was destroyed but pending":** an `asyncio.Task` was
  cancelled but never awaited. `_drain_pending_assignments` covers
  the child-task case; `_run_session`'s finally covers the ceo/tui
  case. Anything else is new territory.
- **Stats panel showing wrong totals after resume:** check the
  `replay=True` skip in `stats.compute`. If a new event kind gets
  re-emitted by `bulk_load`, it needs the tag.
- **Agent compacting every turn:** check `_compaction_floor`. After a
  no-shrink compaction the floor should advance so the next 4 turns
  skip the archivist. Transient failures should NOT advance the
  floor.
- **Symlink/path test fails on macOS:** `O_NOFOLLOW` constant differs
  and `/proc/self/fd` doesn't exist. The code degrades to leaf-only
  protection there; the test suite skips the parent-component check
  when `/proc/self/fd` is absent.
