# Project review — punch list

Working notes from the top-to-bottom reviews on main. Not user-facing
docs — this is the to-do/done file.

**Status (post round-one):** PRs A, B, C, D all merged into main.
Plus PR #16 (user-authored) tuned CONFIG knobs: `temperature=0.1`,
`max_depth=8`, `max_turns_per_agent=1000`, `max_concurrent_llm=2`,
`exec_timeout_seconds=60`, `compact_at_pct=0.6`, `compact_keep_recent=3`.

**Round two (ALL FIXED in this PR — branch `claude/review-round-two`):**
fresh-eyes review of the round-one code (B/C/D modules). Six parallel
reviewers (orchestrator, tool surface, persistence, presentation,
tests+docs, prompts+tool-descriptions). All findings R1-R35 below
addressed; new round-two regression tests at `tests/test_review_rt2.py`.

Bonus: during the work, the `exec_workspace` design question came up
(separate tool vs `workdir` arg vs env var only). Decision: separate
tool, mirroring the existing `read_file`/`read_workspace` and
`write_file`/`write_workspace` splits. Highest discoverability;
matches the agents' mental model of "scratch vs workspace". Env vars
`$TASKGENIE_WORKSPACE` / `$TASKGENIE_SCRATCH` stay as escape hatches
for the rare both-dirs snippet.

---

## Round-two findings — FIXED in this PR

### CRITICAL

#### R1. Symlink TOCTOU in `_safe_path` → arbitrary file overwrite
- **Where:** `taskgenie/tools/work.py:207-216` (`_safe_path`), used by
  `_write_file`, `_write_workspace`, `_edit_workspace`.
- **What:** `.resolve()` runs at validation, the actual `write_text()`
  happens later. In between, a symlink planted at the target path
  redirects the write anywhere. Tool-surface reviewer reproduced this
  end-to-end — overwrote `/etc/passwd` from a scratch base dir via the
  `write_workspace(mode='create')` happy path. PR A2 fixed string-prefix
  containment; this is a separate vector.
- **Fix:** open files with `os.open(..., O_WRONLY | O_CREAT | O_EXCL |
  O_NOFOLLOW)` for create, `O_WRONLY | O_TRUNC | O_NOFOLLOW` for
  overwrite, and `O_RDONLY | O_NOFOLLOW` for reads. For
  `edit_workspace`'s rename, use the same flags on the temp + `os.replace`.
- **Test:** plant a symlink → `/tmp/sentinel` at the target path before
  the write call; assert ValueError/OSError and `/tmp/sentinel`
  unchanged. Run against `_write_file`, `_write_workspace(mode='create')`,
  `_write_workspace(mode='overwrite')`, `_edit_workspace`.

#### R2. Dangling `tool_call_ids` if anything post-`_execute_tool` raises
- **Where:** `taskgenie/agent.py:411-443` (tool pipeline).
- **What:** The `try` only wraps `_execute_tool`. After it returns,
  `json.dumps(result)`, `dump_large_tool_result` (mkdir, write_text),
  `self.messages.append`, and `self._emit` all run unguarded. Any
  failure (disk full, encoding fault, non-JSON-serialisable result, BUS
  subscriber raising) propagates out of `run()` with the assistant turn
  containing all `tool_calls` already appended at line 301 — same
  dangling-id 400 class as PR A1, shifted one layer down. No `RunResult`
  is returned either; parent sees `_tool_await_results`'s blanket
  `except Exception`, agent status never updates.
- **Fix:** wrap from `result_str = ...` through the final `_emit` in a
  try/except; on failure, append a `[tool_pipeline_error]` reply for
  this `tool_call_id`, then seal any remaining ids in the batch before
  marking the agent `error`.
- **Test:** monkeypatch `dump_large_tool_result` to raise; assert
  `agent.messages` has a tool reply for every assistant tool_call_id and
  `result.kind == "error"`.

#### R3. Resume of `done` session re-asks a stale, already-answered question
- **Where:** `taskgenie/__main__.py:558-581`.
- **What:** `last_pause = next(reversed(history) where kind in {question, blocker})`
  doesn't consider whether that pause was already resolved. Flow:
  question → answer → … → done → quit → `--resume`. The check finds the
  long-resolved question (still in history because `mark_answer` only
  appends), branch 1 fires before the `status=="done"` branch on line
  582, user is asked again, CEO ends up with back-to-back answer
  messages. This is exactly the bug the missing B7 test would have
  caught.
- **Fix:** find the index of the most recent `answer`/`unblock`/`done`/
  `followup` first; `last_pause` only considers entries after that.
- **Test:** save a session: question → answer → done. Resume with a
  scripted stdin; assert `_read_followup_input` (not
  `_read_answer_input`) was called.

### HIGH

#### R4. `exec_code` can't reach the workspace (user-flagged)
- **Where:** `taskgenie/tools/work.py:188-204` (schema), `:432` (cwd),
  `taskgenie/prompts.py:357-359` (worker prompt).
- **What:** Both the prompts and tool-surface reviewers flagged this
  independently. Worker writes `game.py` to workspace, then can't run
  it. `cwd` is hardcoded to scratch and `agent.workspace_dir` is never
  exposed in any tool description, schema, or prompt. Workers
  reasonably try `python game.py` and silently fail with "No such
  file"; they waste turns rewriting the file thinking it's broken.
- **Decision:** env-var `TASKGENIE_WORKSPACE` injected into `_scrubbed_env`
  plus schema/prompt update. Smaller surface change than adding a
  `workdir` arg, no new schema field, agents just `cd
  "$TASKGENIE_WORKSPACE"` or use it absolute.
- **Fix:** add `env["TASKGENIE_WORKSPACE"] = str(agent.workspace_dir)` in
  `_exec_code`. Update schema description to document it. Update worker
  prompt at prompts.py:357-359 to mention the env var.
- **Test:** create a workspace file via `_write_workspace`, then
  `_exec_code(language="shell", code='ls "$TASKGENIE_WORKSPACE"')`,
  assert the file appears in stdout.

#### R5. Archivist slice boundary can orphan a `tool` reply
- **Where:** `taskgenie/archivist.py:101-104,197`.
- **What:** `messages[-keep_recent:]` may land on a `{role:"tool"}`
  whose matching `{role:"assistant", tool_calls:[...]}` falls in the
  summarised middle. Next LLM call 400s because the surviving `tool`
  message points at a `tool_call_id` that no longer exists. With the
  default `keep_recent=3` and the typical assistant→tool alternation,
  the slice often lands mid-pair.
- **Fix:** snap the slice forward to the nearest preceding non-`tool`
  message (assistant or user) so the recent band never starts with a
  `tool`.
- **Test:** synthetic 12-message alternating assistant(tool_calls)/tool
  history; compact with keep_recent=3; assert every surviving `tool`
  message's `tool_call_id` exists in some surviving assistant turn.

#### R6. Archivist shares `max_concurrent_llm` semaphore with real agents
- **Where:** `taskgenie/llm.py:29,58` + `taskgenie/archivist.py:136`.
- **What:** At `max_concurrent_llm=1` this is a hard deadlock; at the
  current `=2` it's starvation: when two workers compact simultaneously
  they consume the entire LLM budget, blocking everything else.
- **Fix:** give the archivist its own semaphore (separate budget) OR
  bypass the shared one entirely (archivist is a system task, not a
  user-visible agent).
- **Decision:** bypass — archivist calls don't compete with agents for
  rate-limit purposes since they're triggered by agents themselves.
  Simpler than adding another knob.
- **Test:** with `max_concurrent_llm=1`, kick a compaction concurrently
  with an agent LLM call; assert neither blocks indefinitely.

#### R7. Archivist `parent_id=None` + `depth=99` spawn corrupts hierarchy
- **Where:** `taskgenie/archivist.py:108-119` + `taskgenie/tui.py:312-317`.
- **What:** Archivist publishes `spawn` events on the same bus the TUI
  uses to build the hierarchy. `parent_id=None` makes the TUI overwrite
  `root_id` with the Archivist's id (line 317). Also: stats.compute
  resets per-agent metrics on every spawn it sees — archivist spawns
  pollute the stats panel.
- **Fix:** introduce a distinct event kind (`compaction_start` /
  `compaction_end`) instead of reusing `spawn`/`state`/`complete`. TUI
  surfaces it as a one-line activity log entry on the target agent, not
  as a tree node. Stats ignores it.
- **Test:** archivist runs; assert no new entry in
  `Agent.REGISTRY`, no change to TUI `root_id`, and the target agent's
  activity_log has an entry for the compaction.

#### R8. Archivist compaction wipes the lost-update warning's evidence
- **Where:** `taskgenie/tools/work.py:251-269` (warning scan) +
  `taskgenie/archivist.py:compact_messages` interaction.
- **What:** The round-one PR-A-era lost-update warning at
  `_write_workspace:298` looks for prior `read_workspace` tool_calls in
  `agent.messages`. When the Archivist compacts older messages into a
  single `[archived summary]`, those tool_calls disappear — so an agent
  that reads a file, then compacts, then overwrites it gets no warning.
  Silent regression of a round-one steering fix exactly in the
  long-running sessions PR-D was built for.
- **Fix:** thread a persistent `agent.read_workspace_paths: set[str]`,
  populated in `_read_workspace`, queried by `_write_workspace`.
  Survives compaction.
- **Test:** mock `agent.messages` to contain only `[system, archived,
  user]`; preload `agent.read_workspace_paths`; call `_write_workspace`
  with overwrite; assert warning still fires.

#### R9. `_maybe_compact` re-runs every turn near threshold
- **Where:** `taskgenie/agent.py:201-230`.
- **What:** If a compact succeeds but the new history is still over
  threshold (large recent tool results don't get summarised), every
  subsequent turn pays an extra LLM round-trip. Compounding cost.
- **Fix:** track `self._last_compact_msg_count`; skip if the message
  count has grown by fewer than N (say 4) since the last compact. OR
  refuse to compact if previous compact returned `len(new) > len(old) *
  0.9`. Pick the message-count guard — simpler.
- **Test:** stub `compact_messages` to return input unchanged; run 5
  turns over threshold; assert `compact_messages` invoked at most once.

#### R10. `_is_context_length_error` matches too aggressively
- **Where:** `taskgenie/agent.py:22-37`.
- **What:** Substring `"context length"`/`"context_length"` matches
  innocuous strings like `"context length check passed"` or
  `"warn: context length cache hit 0%"`.
- **Fix:** tighten to anchored patterns (`r"(maximum )?context (window|length).*?(exceeded|too long|exceeded the)"`)
  or test for OpenAI's `context_length_exceeded` code first.
- **Test:** feed innocuous strings with "context" word; assert False.

#### R11. `_tool_fire` doesn't go through `_set_status` — no `state` event
- **Where:** `taskgenie/agent.py:617-626`.
- **What:** Line 621 assigns `child.status = "fired"` directly.
  Subscribers never see the transition; TUI tree shows the agent in its
  previous status until the next event lands.
- **Fix:** `child._set_status("fired", reason=reason)` instead of
  direct assignment.
- **Test:** subscribe to BUS, fire a child, assert a `state` event with
  `status="fired"` and matching agent_id.

#### R12. KeyboardInterrupt snapshot is dead code on Python ≥3.11
- **Where:** `taskgenie/__main__.py:337-340`.
- **What:** `asyncio.run()` in 3.11+ converts SIGINT to `CancelledError`
  on the running task. The `except KeyboardInterrupt` in
  `_drive_session` never fires; users lose in-flight state on Ctrl+C
  contrary to the "[yellow]interrupted[/] state saved" promise.
- **Fix:** catch `(KeyboardInterrupt, asyncio.CancelledError)`,
  snapshot, then re-raise (`raise`).
- **Test:** schedule `loop.call_later(0.1, lambda: signal.raise_signal(SIGINT))`;
  assert state.json exists and contains the post-snapshot data.

#### R13. `_run_session` finally leaves dangling `ceo_task` on TUI exception
- **Where:** `taskgenie/__main__.py:143-150`.
- **What:** If `tui_task` raises (rare but possible on flaky tty), the
  `ceo_task` is left pending. Generates "Task was destroyed but it is
  pending" at shutdown.
- **Fix:** in `finally`, cancel `ceo_task` if not done, gather both
  with `return_exceptions=True`.
- **Test:** make `tui_task` raise; assert no pending tasks after
  `_run_session` returns.

#### R14. `append` mode atomicity claim is wrong for >4KB writes
- **Where:** `taskgenie/tools/work.py:311-317` + comment.
- **What:** The comment claims appends "interleave cleanly at the OS
  write boundary." True only for POSIX `O_APPEND` ≤ `PIPE_BUF` (4096).
  Buffered text-mode `f.write(content)` doesn't translate to one
  `write()` syscall.
- **Fix:** open with `os.open(p, O_WRONLY | O_APPEND | O_CREAT |
  O_NOFOLLOW)` (also fixes symlink TOCTOU for append); single
  `os.write(fd, content.encode())`. For len > PIPE_BUF, drop the
  atomicity claim from the docstring and document the limit.
- **Test:** two concurrent appends of distinct payloads ≤4KB; assert
  the file contains both payloads as contiguous runs (no interleaving).

#### R15. Stats per-agent metrics reset on resume; totals don't
- **Where:** `taskgenie/stats.py:94-99`.
- **What:** `bulk_load` re-publishes a `spawn` event for every
  rehydrated agent. `stats.compute` does `agents[aid] = AgentSummary(...)`
  on each spawn — wiping the pre-resume counts. Aggregated totals are
  increment-only and don't reset, so the panel is internally
  inconsistent post-resume.
- **Fix:** in `compute`, skip the spawn handler when `aid in agents`
  (keep first spawn, ignore replays).
- **Test:** synthetic event log with `spawn → llm_call → spawn` for
  same agent_id; assert agent llm_calls count is preserved.

#### R16. `events.jsonl` is unbounded
- **Where:** `taskgenie/event_logger.py`.
- **What:** No rotation, no max size. `read_events` slurps the whole
  file. With `max_turns_per_agent=1000` and depth-8 hierarchies this is
  100s of MB plausible.
- **Fix:** stream-read in `read_events` (line-by-line). Truncate
  individual event `data` payloads server-side before write if larger
  than N KB. Skip full file rotation for now — premature optimisation.
- **Test:** write a 50MB synthetic log; assert `read_events` peak
  memory stays bounded (just check it returns without OOM in a small
  RSS check, or pass a fake huge generator).

### MEDIUM

#### R17. README + `.env.example` stale on config defaults
- **Where:** `README.md:187-201`, `.env.example`.
- **What:** Stale on 5 user-tuned defaults (temperature, max_depth,
  max_turns, max_concurrent_llm, exec_timeout). Missing all 5 PR-D
  knobs (mcp_call_timeout, ctx_window, compact_at_pct,
  compact_keep_recent, dump_over_bytes). BASE_URL still the Tailscale
  100.x IP. Layout section missing context.py / archivist.py. Test
  list missing test_followup_loop, test_tui, all four test_review_pr*.
- **Fix:** regenerate the env table; update layout + test lists; flip
  base_url to `http://localhost:1234/v1` in both README, `.env.example`,
  AND `config.py`.

#### R18. `Session.save` leaks tempfile on serialization error
- **Where:** `taskgenie/session.py:235-247`.
- **What:** `json.dump(asdict(self.data), tmp, indent=2)` without
  `default=str`. If anything ever leaks a non-JSON-native value,
  serialization raises mid-write, leaving `.state-XXXX.json.tmp` in
  root_dir.
- **Fix:** wrap in `try` with `Path(tmp_path).unlink(missing_ok=True)`
  on failure. Add `default=str` for symmetry with the other JSON
  writers (event_logger, artifacts).
- **Test:** monkeypatch json.dump to raise; assert no `.tg-*tmp*` left
  in the root dir.

#### R19. Atomic write doesn't fsync directory
- **Where:** `taskgenie/session.py:247`.
- **Fix:** after `os.replace`, fsync the directory on POSIX
  (`os.open(self.root_dir, O_RDONLY)` → `os.fsync` → close). Skip on
  Windows.

#### R20. `asyncio.get_event_loop()` deprecated
- **Where:** `taskgenie/event_logger.py:35,37`.
- **What:** DeprecationWarning on 3.12, raises on 3.14+. Project
  requires ≥3.11.
- **Fix:** `asyncio.get_running_loop()`.

#### R21. `drain()` is racy in event_logger
- **Where:** `taskgenie/event_logger.py:31-39`.
- **Fix:** simplest patch: have the writer task set an
  `asyncio.Event` after every flush; `drain` waits on
  `queue.empty() AND last_flush.is_set()` with a small loop.
- **Test:** publish, drain, read events; assert all published events
  visible.

#### R22. TUI activity_log retains un-truncated tool_args
- **Where:** `taskgenie/tui.py:347`.
- **What:** `tool_args=dict(args)` is a shallow copy of the FULL dict.
  Up to 200 entries per agent. A worker doing many large
  `write_workspace` calls holds MBs of redundant content per AgentView.
- **Fix:** in `push_activity`, truncate any string value in `tool_args`
  to ~200 chars (preserve the keys, just clip values). The display
  uses `_short_args` anyway.

#### R23. `_handle_event` empty narrate resets thinking timer
- **Where:** `taskgenie/tui.py:335-340`.
- **Fix:** early-return when `text == ""` before `_touch`.

#### R24. `_ColumnOverflowTree.__rich_measure__` returns invalid bounds at very narrow widths
- **Where:** `taskgenie/tui.py:204`.
- **Fix:** `return Measurement(min(MIN_COL_WIDTH, options.max_width), options.max_width)`.

#### R25. TUI forces alternate-screen even when piped
- **Where:** `taskgenie/tui.py:903`.
- **Fix:** `use_screen = sys.stdout.isatty() and self.console.is_terminal`.

#### R26. Worker prompt misleads about read-then-write warning
- **Where:** `taskgenie/prompts.py:347-349`.
- **What:** Prompt says dispatcher always warns on read-then-write, but
  actually `mode='create'` (default) hard-fails with "file exists"
  while only `mode='overwrite'` triggers the warning.
- **Fix:** rewrite the line to describe both branches accurately.

#### R27. `_run_resume` doesn't snapshot user response
- **Where:** `taskgenie/__main__.py:580, 598, 616`.
- **What:** After `mark_answer`/`mark_followup`/`mark_unblock`, no
  snapshot. If the next `_run_session` crashes, on-disk state never
  records the user's input, causing duplicate prompts on re-resume.
- **Fix:** call `_snapshot(session, ceo)` after each `mark_*` in
  `_run_resume`.

### LOW

#### R28. `ask_user` schema has dead "managers/workers can't see this" text
- **Where:** `taskgenie/tools/management.py:204-239`.
- **Fix:** trim the description tail; only CEOs see the schema.

#### R29. `_seal_tool_batch` trailing-call test path never exercised
- **Where:** `tests/test_review_pra.py:189` —
  `test_complete_seals_trailing_tool_calls_in_same_turn` puts complete
  LAST in the batch, so the trailing-call branch is never run.
- **Fix:** add a case where `complete` is FIRST with a real
  tool_call after it; assert the trailing call's reply is
  `[skipped: agent returned after complete...]`.

#### R30. Leaky test fixtures
- **Where:** `test_review_pra.py:142-147`, `test_review_prb.py:113-118`,
  `test_review_prb.py:128/172/206/228`.
- **What:** `with tempfile.TemporaryDirectory() as d: ... return CEO`
  — tempdir destroyed before the test runs. CONFIG.data_dir mutations
  without try/finally restore.
- **Fix:** use `tmp_path` pytest fixture; wrap CONFIG mutations.

#### R31. `test_loop.py` fixture text claims `exec_code is stubbed in v1`
- **Where:** `tests/test_loop.py:316`.
- **Fix:** delete/update the comment.

#### R32. `dump_large_tool_result` filename collision risk on long call_ids
- **Where:** `taskgenie/context.py:95-97`.
- **Fix:** include a short hash of the full call_id in the filename.

#### R33. `_safe_path` NUL-byte validation
- **Where:** `taskgenie/tools/work.py:207-216`.
- **Fix:** reject `\x00` in the relative path upfront with a clean
  ValueError.

#### R34. EventKind Literal incomplete
- **Where:** `taskgenie/events.py:8-21`.
- **Fix:** add `"blocked"`, `"question"`, `"follow_up_suggested"`,
  `"llm_call"`, `"tool_result"` (already emitted by agent.py).

#### R35. TwoTurnLLM test assertion is a tautology
- **Where:** `tests/test_followup_loop.py:123`.
- **Fix:** `len(mock.complete_reports) >= 0` is always true. Drop or
  populate.

---

## Round-one history (collapsed)

### PR A — Critical bugs (MERGED #12)
A1 dangling tool_call_ids on early return; A2 path traversal via
`startswith` in `_safe_path`; A3 Rich markup injection in event log.

### PR B — Resume hardening + tests (MERGED #13)
B1 `Session.load` defensive; B2 ceo-bucket in tool-signature diff; B3-B7
test gaps.

### PR C — Resource safety (MERGED #14)
C1 MCP `call_tool` timeout; C2 `exec_code` subprocess group kill; C3 MCP
close logs cleanly; C4 `edit_workspace` temp cleanup; C5 (deferred,
bus queues unbounded — touched again in R16).

### PR D — Context-window management (MERGED #15)
D1 inline tool-output dumping (16KB threshold); D2 Archivist; D3 pre-call
compaction; D4 reactive recovery on context-length errors.

---

## Bright spots — preserve

- `_safe_jsonable` in `artifacts.py:74-107` — recursive sanitizer with `repr()` fallback.
- `_seal_tool_batch` (agent.py:451) — correct sealing across all early-return paths from PR A.
- `_ColumnOverflowTree` in `tui.py:127-203` — measure-first, cell-aware column packing.
- `RunResult.to_tool_payload` in `result.py` — decouples internal state from parent-visible JSON.
- Worker prompt's "honesty" section (`prompts.py:273-303`) — concrete failure modes.
- Manager prompt's "be honest with your parent" block (`prompts.py:240-259`).
- `edit_workspace` schema (`work.py:130-167`) — failure modes enumerated with recovery actions.
- `_snapshot_initial_agents` two-pass logic + explanatory comment (`tui.py:253-296`).
- `test_workspace.py::test_utf8_roundtrip` — covers cp1252 fallback with CJK/arrows/math/emoji.
