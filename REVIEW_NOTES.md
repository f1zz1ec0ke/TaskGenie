# Project review — punch list

Working notes from the top-to-bottom review on main. Not user-facing
docs — this is the to-do/done file.

**Status (post round-one):** PRs A, B, C, D all merged into main.
Plus PR #16 (user-authored) tuned CONFIG knobs: `temperature=0.1`,
`max_depth=8`, `max_turns_per_agent=1000`, `max_concurrent_llm=2`,
`exec_timeout_seconds=60`, `compact_at_pct=0.6`,
`compact_keep_recent=3`. Tuned for deep, long-running tasks with
more aggressive compaction.

**Next pass (round two):** another top-to-bottom review against the
new main. Same five-agent parallel-review pattern as before
(orchestrator, tool surface, persistence, presentation, tests+docs).
**This pass MUST include code from PRs B/C/D since I wrote it; fresh
eyes are overdue.** Use the previous review's findings as a
calibration baseline — what we missed, what we over-stated.

---

## PR D — Context-window management (MERGED #15 + #16)

Branch: `claude/review-pr-d-context`
Status: merged

Rescoped during planning: the original "CEO history compaction" framing
was wrong (CEO sees only subordinate FINAL reports via to_tool_payload,
not their full histories). The real risk is workers with huge
tool-result tail (read_workspace + exec_code outputs). Final scope:

### D1. Inline tool-output dumping (MERGED)
### D2. Archivist (MERGED) — a "background agent" emitting bus events
### D3. Pre-call compaction (MERGED)
### D4. Reactive recovery on context-length error (MERGED)

---

## PR A — Critical bugs (MERGED #12)

Branch: `claude/review-pr-a-critical`
Status: merged

### A1. Dangling tool_call_ids after early return (CRITICAL, verified)
- **Where:** `taskgenie/agent.py:228-288`
- **What:** The for-loop appends the assistant turn (with all `tool_calls`) to `self.messages` at line 204, then iterates. The branches for `complete`, `report_blocker`, `ask_user` `return` early without appending a `{"role":"tool", "tool_call_id":…}` reply for that call (or any trailing calls in the same batch). Next time those messages are sent to the API — which now happens every follow-up turn — the API will 400 because the conversation has a dangling `tool_call_id`.
- **Fix:** add a helper that seals all unanswered tool_call_ids before each early return.
- **Test:** ScriptedLLM returns complete; assert `ceo.messages[-1]` is a `tool` role matching the complete's `tool_call_id`. Same for blocker, ask_user.

### A2. Path traversal via `startswith` in `_safe_path` (HIGH, verified)
- **Where:** `taskgenie/tools/work.py:206-212`
- **What:** `str(target).startswith(str(base))` accepts sibling directories that share a name prefix. With `base = /tmp/scratch`, target = `/tmp/scratch-evil/foo` passes the check.
- **Fix:** `target.relative_to(base)` in `try/except ValueError`, raise on ValueError.
- **Test:** call `_safe_path("/tmp/scratch", "../scratch-evil/foo")` and assert `ValueError`.

### A3. Rich markup injection in event log (HIGH, verified)
- **Where:** `taskgenie/tui.py:839` — `Text.from_markup("\n".join(self.recent_events))`
- **What:** Agent-supplied strings (names, tool args, narrations, blocker reasons, feedback, questions) get interpolated raw into f-string log lines, then re-parsed as Rich markup at render time. An LLM emitting `[red]boom[/]` or `[link=file://...]` gets re-interpreted on screen 2. Local-only, but garbled output + potential terminal-escape weirdness.
- **Fix:** escape agent-supplied fields with `rich.markup.escape()` at the f-string call sites in `_handle_event` before they enter `_log()`. Keep our own markup tags intact.
- **Test:** call `_log()` indirectly via `_handle_event` with an agent name containing `[red]X[/]`, render the log panel, assert the rendered string contains the literal `[red]X[/]` (not stripped/styled).

---

## PR B — Resume hardening + test gaps (MERGED #13)

Branch: `claude/review-pr-b-resume`
Status: merged

### B1. Session.load has no error handling (HIGH, verified)
- **Where:** `taskgenie/session.py:159-165`
- **What:** Reads `state.json` and unpacks via `SessionData(**raw)`. Torn writes, hand-edited files, or extra fields from a newer version all raise on load with no friendly message.
- **Fix:** filter `raw` to known dataclass fields, wrap construction in try/except, log + raise a friendly error.
- **Test:** save a session, append a junk key to state.json, attempt resume, assert helpful error or graceful skip.

### B2. `diff_tools_against` ignores CEO-only tools (HIGH, agent-reported, verify before fixing)
- **Where:** `taskgenie/session.py:269-304` + `taskgenie/tools/management.py`
- **What:** `ask_user` is registered with `kind="ceo"` and never makes it into the persisted `management`/`work` signatures. Schema changes to ceo-only tools won't be flagged on resume.
- **Verify:** read management.py to confirm `ask_user` registration kind.
- **Fix:** persist a third `"ceo"` bucket and diff it.
- **Test:** change ask_user schema between save and resume, assert the diff surfaces it.

### B3. Missing test: LLM transport failure → error RunResult
- **Where:** `taskgenie/agent.py:188-192`
- **Test sketch:** ScriptedLLM whose `chat` raises on turn 1; assert `result.kind == "error"` and that `__main__` writes stats + rc=1.

### B4. Missing test: turn-limit exhaustion
- **Where:** `taskgenie/agent.py:323-327`
- **Test sketch:** ScriptedLLM that always returns a no-op narration with no tool_calls; monkeypatch `CONFIG.max_turns_per_agent=3`; assert `kind="exhausted"`.

### B5. Missing test: `max_depth` cap on hire
- **Where:** `taskgenie/agent.py:367-373`
- **Test sketch:** chain agents to `max_depth - 1`, hire one more, expect an error response, assert no new agent in REGISTRY.

### B6. Missing test: malformed JSON in tool args
- **Where:** `taskgenie/agent.py:230-238`
- **Test sketch:** ScriptedLLM returns a tool_call with `arguments="not json"`; assert agent continues and appends `[tool_args_error]` to messages.

### B7. Missing test: resume of a `done` session uses the follow-up flow
- **Where:** `taskgenie/__main__.py:578-595` (approx)
- **Test sketch:** save a session in `done` state, resume, assert the follow-up prompt was used (not blocker prompt).

---

## PR C — Resource safety (MERGED #14)

Branch: `claude/review-pr-c-resources`
Status: merged

### C1. MCP `call_tool` has no timeout (MEDIUM, agent-reported)
- **Where:** `taskgenie/mcp_client.py:117-119`
- **Fix:** wrap `session.call_tool` in `asyncio.wait_for(..., CONFIG.mcp_call_timeout_seconds)`. Add config knob with sane default (30s).
- **Test:** monkeypatch a mock MCP session whose `call_tool` hangs; assert timeout returns an error string.

### C2. `exec_code` doesn't kill subprocess group (MEDIUM, agent-reported)
- **Where:** `taskgenie/tools/work.py:413-426`
- **Fix:** `start_new_session=True` on `create_subprocess_exec`, then `os.killpg(proc.pid, SIGKILL)` on timeout.
- **Test:** code that backgrounds `sleep 9999 &`, time-out, then assert no leftover sleep process (best-effort).

### C3. MCP `close()` swallows all exceptions silently (LOW-MEDIUM, agent-reported)
- **Where:** `taskgenie/mcp_client.py:140-144`
- **Fix:** log the exception (`logging.warning` or print to stderr) and keep best-effort behavior.

### C4. `edit_workspace` temp file leak on partial failure (LOW, agent-reported)
- **Where:** `taskgenie/tools/work.py:347-349`
- **Fix:** `try/finally` removing the `.tg-edit-tmp` if it still exists.

### C5. Bus queues are unbounded (LOW, agent-reported, defer)
- **Where:** `taskgenie/events.py:48-50`
- **Defer:** only bites if a subscriber forgets `unsubscribe`. Current code is careful with `finally`. Bounded queue + drop-oldest is the right answer but no rush.

---

## PR D — Context-window management (IN FLIGHT)

Branch: `claude/review-pr-d-context`
Status: opened for review

Rescoped during planning: the original "CEO history compaction" framing
was wrong (CEO sees only subordinate FINAL reports via to_tool_payload,
not their full histories). The real risk is workers with huge
tool-result tail (read_workspace + exec_code outputs). New scope:

### D1. Inline tool-output dumping
- `taskgenie/context.py:dump_large_tool_result()` — tool replies over
  `CONFIG.dump_tool_output_above_bytes` (default 4KB) get written to
  the agent's scratch dir; the in-memory message becomes a one-line
  pointer telling the agent to `read_file` if they need the full text.
  Generic for all tools including MCP.

### D2. Archivist agent
- `taskgenie/archivist.py:compact_messages()` — when invoked, runs an
  LLM call (NOT a full Agent — direct LLM with bus events for TUI
  visibility) that compresses the middle band of an agent's messages
  into one `[archived summary]` user message. Preserves: system
  prompt, original task, last N turns. Falls back to original on LLM
  failure.

### D3. Pre-call compaction
- `Agent._maybe_compact()` invoked before every `LLM.chat`. If
  `count_tokens(self.messages) > CONFIG.ctx_window_tokens * CONFIG.compact_at_pct`,
  the Archivist runs and the result is swapped in.

### D4. Reactive recovery
- If `LLM.chat` raises a context-length error, `_is_context_length_error`
  pattern-matches the message and forces a compact + one retry.

---

## Other findings (verify + queue)

- **README default `TASKGENIE_BASE_URL`** is a Tailscale 100.x address. Likely your machine, not a useful default. Update README + `config.py` to `http://localhost:1234/v1`.
- **README test list** at README.md ~line 273 missing `test_followup_loop.py` + `test_tui.py`.
- **test_loop.py fixture text** at line ~316 claims `exec_code is stubbed in v1` — contradicts the README's "Real exec_code" claim. Fixture only, update for accuracy.
- **Layout misbehavior at small terminal sizes** (tui.py) — body collapses to 1 row. Low priority; add a "terminal too small" fallback panel below some min size.

---

## Bright spots — preserve

- `_safe_jsonable` in `artifacts.py:74-107` — recursive sanitizer with `repr()` fallback.
- `save()` in `session.py:200-220` — fsync + same-dir tempfile + `os.replace`. Textbook atomic write.
- `_ColumnOverflowTree` in `tui.py:127-203` — measure-first via uncapped `render_lines`, then cell-aware column packing.
- `RunResult.to_tool_payload` in `result.py` — decouples internal state from parent-visible JSON.
- `test_workspace.py::test_overwrite_after_read_warns` — meaningful test for a subtle lost-update steering bug.
- `test_workspace.py::test_utf8_roundtrip` — covers a real cross-platform bug class (cp1252 fallback) with CJK, arrows, math, emoji round-trip.

---

## Done / merged

(none yet — PR A in flight)
