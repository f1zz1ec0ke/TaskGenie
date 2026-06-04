# TaskGenie Architecture

## High-level overview

TaskGenie is a hierarchical multi-agent system that decomposes tasks through delegation. There is one `Agent` class; the hierarchy emerges from the `role_type` field and the prompts/tools each agent receives:

```
CEO (role_type="ceo")           -- delegates only, never does work
  Manager (role_type="manager") -- decomposes, hires workers, reviews
    Worker (role_type="worker") -- calls tools to produce output
```

The CEO is created at startup with a fixed system prompt (`CEO_ROLE_PROMPT` in `prompts.py`). Managers and workers are created at runtime when a parent calls the `hire` tool, which writes a custom role prompt that becomes the child's system prompt. The prompt templates (`MANAGER_ROLE_PROMPT_TEMPLATE`, `WORKER_ROLE_PROMPT_TEMPLATE`) inject the agent's name, parent name, and charter.

All agents live in `Agent.REGISTRY`, a class-level `dict[str, Agent]`. The parent-child tree is maintained via `Agent.children` and `Agent.parent` pointers.

## Agent loop

The core loop lives in `Agent._run_loop` (`agent.py`). Simplified:

```
for turn in range(max_turns_per_agent):
    maybe_compact()                    # pre-call context trimming
    response = LLM.chat(messages, tools)
    messages.append(assistant_message)

    if no tool_calls:
        nudge the model; after 2 empty turns, auto-complete

    for each tool_call:
        if complete     -> return RunResult(kind="done")
        if report_blocker -> return RunResult(kind="blocked")
        if ask_user     -> return RunResult(kind="question")
        else            -> dispatch tool, append result message

return RunResult(kind="exhausted")
```

Key details:
- **Parallel delegation**: `assign` wraps `child.run()` in an `asyncio.Task` and returns a handle immediately. `await_results` gathers multiple handles. This lets a manager fan out work concurrently.
- **Context-length recovery**: If the LLM returns a context-window error, the loop force-compacts and retries once.
- **Seal-on-exit**: When `complete`/`report_blocker`/`ask_user` fires mid-batch, `_seal_tool_batch` appends stub tool replies for any remaining tool calls in that batch so the message history stays valid for the API.
- **Drain on terminal exit**: `_drain_pending_assignments` cancels in-flight child tasks when an agent reaches a terminal state (done/error/exhausted), bounded by a 2-second timeout.

## Event system

`events.py` defines a global `EventBus` (`BUS`) with bounded async queues.

```python
q = BUS.subscribe(maxsize=10_000)   # returns asyncio.Queue[Event]
BUS.publish(Event("spawn", agent_id, agent_name, data={...}))
```

When a queue fills up, `publish` drops the oldest event (never blocks the publisher). Event kinds include: `spawn`, `narrate`, `tool_call`, `tool_result`, `hire`, `assign`, `review`, `fire`, `rehire`, `complete`, `error`, `state`, `llm_call`, `blocked`, `question`, `follow_up_suggested`, `compaction_start`, `compaction_end`.

Subscribers: the TUI (renders the live display), the `JSONLEventLogger` (appends to `events.jsonl` for post-run stats), and the `StoryNarrator` (feeds events to the LLM for sarcastic commentary).

## Context management

Two mechanisms keep agents from blowing the context window (`context.py` + `archivist.py`):

**Token counting** -- A 3-chars-per-token heuristic (`count_tokens`) estimates message list size. `should_compact` checks whether the estimate exceeds `compact_at_pct` (default 80%) of `ctx_window_tokens` (default 128k).

**Large tool output dumping** -- `dump_large_tool_result` writes any tool response larger than `dump_tool_output_above_bytes` (default 128KB) to chunk files in the agent's scratch dir, replacing the in-memory message with a pointer string. The agent can retrieve the full text later via `read_file`.

**Archivist compaction** -- When the threshold is crossed, `compact_messages` (`archivist.py`) splits the message history into three bands:
1. **Head**: system prompt + original task (preserved verbatim)
2. **Middle**: everything between head and recent (summarized)
3. **Recent**: last `compact_keep_recent` messages (preserved verbatim, snapped forward to avoid orphaning tool_call_ids)

The middle band is rendered into a transcript and sent to the LLM with `ARCHIVIST_SYSTEM_PROMPT`, which asks for a dense 200-word summary. The result is spliced back as a single `[archived summary]` user message.

A repeat-compaction guard (`_compaction_floor`) prevents the agent from re-invoking the archivist every turn when one compaction didn't bring the history below threshold. Compaction stats (count, skipped, failed, tokens saved, duration) are tracked per-agent and surfaced to the parent in `RunResult`.

## Session persistence

Each session lives at `.taskgenie/sessions/<id>/` with two files:

- **`state.json`** -- Full snapshot: `SessionData` (task, status, history) + serialized agent tree (`Agent.to_dict()` for every agent in `REGISTRY`) + tool signature hashes. Written atomically via `tempfile` + `os.replace` + `fsync`.
- **`events.jsonl`** -- Append-only event log written by `JSONLEventLogger`. Used for post-run stats computation, not for replay.

Snapshots happen at quiescent points: after the CEO's `run()` returns (done, blocked, question, error). On resume (`--resume`), `Agent.bulk_load` rehydrates the agent tree and re-emits spawn/state events tagged `replay=True` so the TUI can paint the restored hierarchy while the event logger can ignore replays.

User-facing deliverables live separately at `./TaskGenie/<project>/`. The CEO's report is also written there as `README.md`.

## Tool system

`tools/registry.py` defines `ToolRegistry` (singleton `REGISTRY`). Each tool has a schema, a `ToolKind`, a version string, a source tag, and an optional async dispatcher.

**Three tool buckets**:
- **Management** (`kind="management"`): `hire`, `assign`, `await_results`, `review`, `rehire`, `fire`. Given to CEO and managers. Dispatched directly by `Agent._execute_tool`.
- **Work** (`kind="work"`): `read_file`, `write_file`, `exec_code`, `write_workspace`, `edit_workspace`, `exec_workspace`, `copy_to_workspace`, `copy_from_workspace`, plus any MCP tools. Given to workers. Dispatched via registered callables.
- **Both** (`kind="both"`): `complete`, `report_blocker`, `suggest_follow_up`, `list_workspace`, `read_workspace`. Available to all roles.
- **CEO-only** (`kind="ceo"`): `ask_user`. Only the CEO can pause the run to prompt the human.

Tool schemas carry a `schema_hash` (SHA-256 of the normalized JSON). On session resume, `Session.diff_tools_against` compares saved hashes against the current registry to surface added/removed/changed tools.

**MCP integration** -- `MCPClient` (`mcp_client.py`) reads `mcp.json`, spawns stdio MCP servers, and registers their tools as work tools. Each MCP tool call goes through `session.call_tool` with a configurable timeout. Name collisions are resolved by prefixing with the server name.

## TUI

The terminal UI (`tui.py`) is built on Rich's `Live` layout, refreshing at 8 fps. It subscribes to `BUS` and maintains a parallel `AgentView` tree that mirrors the agent hierarchy.

**Four screens** (cycled with Tab or number keys):
1. **Focus + Tree** (default) -- Split panel. Left: focused agent's status, task, activity log, compaction summary, follow-ups. Right: hierarchy tree with worker roll-ups and a spotlight on the most recently active worker. The tree uses `_ColumnOverflowTree` to flow into multiple columns when it exceeds the panel height.
2. **Event Log** -- Scrollable markup log of all bus events.
3. **Story Mode** -- Sarcastic narration from `StoryNarrator` (updated every ~20 seconds when >= 5 new events have accumulated).
4. **Help** -- Keyboard shortcut reference.

**Keyboard shortcuts**: `Tab` cycle screens, `1`/`2`/`3` direct screen select, `?`/`h` help, `j`/`k` cycle focus, `f` toggle fired agents. Input is handled via termios cbreak on Unix (preserving Ctrl+C for SIGINT) and `msvcrt.kbhit` polling on Windows.

## LLM client

`LLM` (`llm.py`) is a singleton `LMStudioClient` wrapping `AsyncOpenAI`. It talks to any OpenAI-compatible endpoint (LM Studio, Anthropic, OpenAI, etc.) configured via `TASKGENIE_BASE_URL` and `TASKGENIE_API_KEY`.

- **Concurrency**: An `asyncio.Semaphore` (default 2) throttles concurrent agent LLM calls so fan-out from a manager hiring N workers doesn't overwhelm the model server. The Archivist and StoryNarrator bypass the semaphore (`bypass_semaphore=True`) so compaction never deadlocks against normal agent work.
- **Retry**: Up to 3 retries with exponential backoff + jitter for transient errors (`APITimeoutError`, `APIConnectionError`, `InternalServerError`, `RateLimitError`).
- **Multi-model**: Per-role model overrides via `TASKGENIE_CEO_MODEL`, `TASKGENIE_MANAGER_MODEL`, `TASKGENIE_WORKER_MODEL`. The agent passes `model_override` to `LLM.chat`; falls back to auto-detection (first model at the endpoint) if nothing is configured.

## Workspace

The shared workspace (`./TaskGenie/<project>/`) is the directory of deliverable files the user keeps after a run. All agents can read it; workers can write to it.

**Optimistic concurrency via `edit_workspace`**: Instead of whole-file overwrite, `edit_workspace` takes `(path, old_text, new_text)` and replaces exactly one occurrence atomically (via `mkstemp` + `os.replace`). If `old_text` is not found (another worker changed the file), or appears multiple times (ambiguous), the tool returns an error and the agent must re-read and retry. This surfaces concurrent-edit conflicts as explicit errors rather than silent data loss.

**Lost-update detection**: `Agent.read_workspace_paths` tracks which workspace files an agent has read. If the same agent later calls `write_workspace(mode='overwrite')` on a file it previously read, the tool appends a warning nudging toward `edit_workspace`. This set persists across Archivist compaction.

**File safety**: All file I/O goes through `_safe_path` (path-escape prevention) and `_safe_open` (leaf symlink rejection via `O_NOFOLLOW`, hardlink detection via `fstat` + `st_nlink`, parent-component TOCTOU re-check via `/proc/self/fd`). This is best-effort hardening, not a security sandbox.

**Two exec tools**: `exec_code` (CWD = agent's private scratch) for intermediate computation, and `exec_workspace` (CWD = shared workspace) for running/testing deliverables. Both use a scrubbed environment and configurable timeout with process-group kill on expiry.
