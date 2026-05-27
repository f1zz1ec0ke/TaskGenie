# Project review — punch list

Working notes from the top-to-bottom reviews on main. Not user-facing
docs — this is the to-do/done file.

**Status (post round-one):** PRs A, B, C, D all merged into main.
Plus PR #16 (user-authored) tuned CONFIG knobs: `temperature=0.1`,
`max_depth=8`, `max_turns_per_agent=1000`, `max_concurrent_llm=2`,
`exec_timeout_seconds=60`, `compact_at_pct=0.6`, `compact_keep_recent=3`.

**Round two (MERGED PR #18):** fresh-eyes review of the round-one code
(B/C/D modules). Six parallel reviewers caught R1-R35; all addressed
with `tests/test_review_rt2.py` covering them. The `exec_workspace`
split (vs `workdir` arg vs env var only) was a design call landed
during the work.

**Round three (IN FLIGHT — single PR on branch `claude/review-round-three`):**
six more parallel reviewers against the post-round-two code. Three
critical security findings reproduced end-to-end (hardlink + parent-
component TOCTOU surface that `O_NOFOLLOW` doesn't cover), one
critical correctness bug (my R18 fix was broken — `tmp_path` captured
AFTER `json.dump`, so the cleanup branch was skipped on the exact
serialization failure R18 claimed to handle), and several round-2-fix
incompletions found by two reviewers independently (compaction_floor
not persisted across rehydrate; tool-args/event-log truncation
doesn't recurse into nested dicts).

---

## Round-three findings — FIXED in this PR

### CRITICAL — security (reproduced end-to-end by reviewers)

#### R36. Hardlink write-through bypasses `O_NOFOLLOW`
- **Where:** `taskgenie/tools/work.py` — `_open_nofollow` and all
  write callers (`_write_file`, `_write_workspace` all modes,
  `_edit_workspace`).
- **What:** `O_NOFOLLOW` only refuses SYMBOLIC links, not hardlinks. A
  hardlink shares an inode with another file but has its own dirent;
  `open(..., O_NOFOLLOW)` succeeds because the dirent isn't a symlink.
  Reviewer reproduced: `ln /etc/passwd ./harmless.md` (no `-s`) then
  `write_workspace(mode='overwrite', path='harmless.md')` overwrote
  `/etc/passwd`.
- **Fix:** after opening with `O_NOFOLLOW`, `fstat` the fd and refuse
  if `st_nlink > 1`. The check is one syscall and catches all hardlink
  vectors. Same fix covers R38 (read disclosure).
- **Test:** plant a hardlink to a sentinel outside the workspace,
  attempt `_write_workspace(mode='overwrite')`, assert sentinel
  unchanged.

#### R37. Parent-component TOCTOU still exploitable
- **Where:** `taskgenie/tools/work.py` — `_safe_path` returns a Path
  whose intermediate components may not yet exist; `_open_nofollow`
  only protects the LEAF.
- **What:** Reviewer reproduced: validate `subdir/file.txt` (subdir
  doesn't exist), plant `subdir` as a symlink to `/tmp`. The
  subsequent `mkdir(parents=True, exist_ok=True)` accepts the
  symlink-as-dir, and `open(..., O_NOFOLLOW)` happily traverses the
  parent symlink because `O_NOFOLLOW` is leaf-only. `/tmp/file.txt`
  was overwritten.
- **Fix:** after open, fetch the realpath of the open fd
  (`/proc/self/fd/N` on Linux) and re-verify containment against
  `base.resolve()`. If the open fd resolves outside `base`, refuse
  and rollback. Skip on platforms without `/proc/self/fd`.
- **Test:** validate `subdir/file.txt`, plant `subdir` as symlink to
  a foreign sentinel directory, attempt the write, assert the
  sentinel-dir file is NOT created.

#### R38. Hardlink-read = arbitrary file disclosure
- **Where:** `taskgenie/tools/work.py` — `_read_text_safe`, used by
  `_read_file`, `_read_workspace`, `_edit_workspace`.
- **What:** Same root cause as R36; reviewer read `/etc/hostname` via
  `ln /etc/hostname ./info.md` + `_read_workspace(path='info.md')`.
  Returned `'vm\n'`.
- **Fix:** same `fstat + st_nlink == 1` check on the read fd.

### CRITICAL — correctness

#### R39. Session.save tempfile leak still happens (my R18 fix is broken)
- **Where:** `taskgenie/session.py:239-273`.
- **What:** `tmp_path = tmp.name` is captured AFTER `json.dump` +
  `tmp.flush` + `os.fsync`. If any of those raises (the exact failure
  R18 claimed to handle), the `finally` block sees `tmp_path = None`
  and skips cleanup. The orphaned `.state-XXXX.json.tmp` file is
  exactly what R18 was supposed to prevent.
- **Fix:** move `tmp_path = tmp.name` to immediately after the
  `NamedTemporaryFile` opens, BEFORE the write.
- **Test:** monkeypatch `json.dump` to raise mid-write; assert no
  `.state-*.json.tmp` left in the session root dir.

### HIGH — multiple-reviewer corroboration

#### R40. `_compaction_floor` not persisted across rehydrate
- **Where:** `taskgenie/agent.py` — `to_dict`/`_rehydrate_one`.
- **What:** Both orchestrator H1 and persistence L1 found this. After
  resume, the floor resets to 0; the repeat-compact guard from R9 is
  silently disabled until 4 new messages accumulate. Long resumed
  sessions pay an extra Archivist LLM call on the very next turn,
  often pointless because compaction just happened pre-pause.
- **Fix:** add `compaction_floor` to `to_dict()`; restore in
  `_rehydrate_one` with sensible default (we use `len(messages)` so
  old snapshots without the field don't immediately re-compact).

#### R41 + R42. Truncation doesn't recurse (event_logger AND TUI)
- **Where:** `taskgenie/event_logger.py:_truncate_for_log` and
  `taskgenie/tui.py:_handle_event` tool_call branch.
- **What:** Both persistence M1 and presentation H1 found this.
  R16/R22 truncation only checks `isinstance(v, str)`. But the most
  realistic offender is `tool_call.args`, which is a DICT — the
  truncator sees a dict, skips it, and the full payload writes to
  events.jsonl (R16) AND lives in `view.activity_log` (R22). A
  `write_workspace(content='<10MB>')` defeats both fixes today.
- **Fix:** recurse through dicts and lists, applying the cap to each
  string leaf. Bytes too.

### HIGH — single-reviewer

#### R43. `_safe_recent_keep` silent-no-op + floor-advance trap
- **Where:** `taskgenie/archivist.py:_safe_recent_keep` +
  `taskgenie/agent.py:_maybe_compact`.
- **What:** On a degenerate alternating history (assistant(tc) → tool
  → assistant(tc) → tool …), the snap pushes `keep_recent` so far
  forward that `len(middle) < 2`, so `compact_messages` returns the
  input unchanged. But `_maybe_compact` still advances
  `_compaction_floor`, so compaction never retries — permanently
  disabled for that agent. No telemetry surfaces the cause.
- **Fix:** `compact_messages` returns a sentinel (or raises a typed
  exception) when the snap forces a no-op, so `_maybe_compact` can
  emit a `compaction_end(success=False, reason="recent-band-too-fat")`
  AND skip advancing the floor.

#### R44. `compact_messages` assumes `original_task.role == "user"`
- **Where:** `taskgenie/archivist.py:117-124`.
- **What:** Fragile under any future code path that pre-populates
  messages with an assistant turn at index 1. Today guaranteed safe
  but no validation.
- **Fix:** assert/validate; on violation, no-op compact with a clear
  reason.

#### R45. `_maybe_compact` can orphan `compaction_start` on raise
- **Where:** `taskgenie/agent.py:_maybe_compact`.
- **What:** If `compact_messages` raises mid-call (especially
  `CancelledError` from `_tool_fire` while the parent fires this
  agent), `compaction_end` is never emitted. TUI keeps showing
  "archivist: compacting N messages" forever and the floor never
  advances correctly.
- **Fix:** wrap the body in try/finally so `compaction_end` always
  emits (with a reason if it failed); re-raise `CancelledError`.

#### R46. `bypass_semaphore` rationale is wrong; uncapped fanout DOS risk
- **Where:** `taskgenie/llm.py` + `taskgenie/archivist.py`.
- **What:** The comment claims the calling agent already holds the
  LLM semaphore, so bypassing avoids self-deadlock. False:
  `_maybe_compact()` runs BEFORE `LLM.chat(self.messages)`, so the
  caller does NOT hold the semaphore during the compact call. With
  N concurrent agents crossing the threshold (managers fanning out
  workers), N uncapped archivist calls hit a local LM Studio
  simultaneously — easy DOS for a single-GPU host.
- **Fix:** add a separate `_archivist_sem` to `LMStudioClient` with
  its own (small) cap; `bypass_semaphore=True` routes through it
  instead of skipping entirely. Default: `max(1, max_concurrent_llm)`.

#### R47. `_compaction_floor` advances on transient archivist failures
- **Where:** `taskgenie/agent.py:_maybe_compact`.
- **What:** When `compact_messages` returns input unchanged (which
  happens on transient LLM blip OR genuine no-op), the floor advances
  regardless — blocks retryable failures for 4 turns. Conflates two
  outcomes.
- **Fix:** distinguish "transient failure" from "no-op" via the
  return value or a typed exception; only advance the floor on
  genuine no-op.

#### R48. `bulk_load` re-emits `blocked` events → double-counted stats
- **Where:** `taskgenie/agent.py:bulk_load` (`blocked` republish) +
  `taskgenie/stats.py:compute`.
- **What:** R15 only handled `spawn` replays; `bulk_load` also
  re-publishes a `blocked` event for any agent in blocked state. The
  event lands in `events.jsonl` and `stats.compute` increments
  `blockers_total` AND `blockers_escalated_to_user` (for the CEO)
  every resume.
- **Fix:** tag the re-emitted events with `data["replay"] = True`;
  `stats.compute` skips replayed events for counter purposes.

#### R49. Worker prompt R26 rewrite has a grammar bug
- **Where:** `taskgenie/prompts.py:348-354`.
- **What:** "tracks which paths you previously read_workspace'd"
  binds to BOTH the create-fail AND the overwrite-warning branches.
  Only the overwrite WARNING is conditional on the tracker; create
  hard-fails on `FileExistsError` regardless.
- **Fix:** rewrite to clearly separate the two safety nets.

#### R50. Worker prompt R8 rule is wrong post-compaction
- **Where:** `taskgenie/prompts.py:333-338`.
- **What:** "If you have a `read_workspace` call in your history for
  a path, your write back MUST be `edit_workspace`" — but R8's
  persistent set survives compaction even after the message-history
  read is gone. A worker who "doesn't recall" reading a file may
  still get the lost-update warning and be confused.
- **Fix:** rewrite to describe INTENT not message-history forensics.

#### R51. `exec_workspace` description mis-routes file-generating snippets
- **Where:** `taskgenie/tools/work.py:_EXEC_WORKSPACE_SCHEMA`.
- **What:** "If your team has not produced any workspace files yet,
  … you probably want `exec_code` instead" is wrong for the legit
  case of GENERATING files (`npm init`, `git clone`, `cookiecutter`).
- **Fix:** rewrite to call out the generator case explicitly.

### MEDIUM

#### R52. `_agent_read_workspace_paths` union picks up FAILED reads
- **Where:** `taskgenie/tools/work.py:_agent_read_workspace_paths`.
- **What:** Persistent set is populated only on successful reads;
  message-scan picks up the read tool_call regardless of whether
  the read errored. False-positive lost-update warning on fresh
  files the worker only "tried to read".
- **Fix:** drop the union, rely on the persistent set; update test
  fakes to populate the set directly.

#### R53. `edit_workspace` tempfiles surface in `list_workspace`
- **Where:** `taskgenie/tools/work.py:_list_workspace`.
- **Fix:** filter `*.tg-edit-tmp` entries.

#### R54. `list_workspace`'s `is_file()` follows symlinks
- **Where:** `taskgenie/tools/work.py:_list_workspace`.
- **Fix:** `p.is_file()` → `p.lstat()` + `stat.S_ISREG` check; surface
  symlinks distinctly (`→ target`) so unexpected planted links are
  visible to the operator.

#### R55. `_is_context_length_error` misses Anthropic body shape
- **Where:** `taskgenie/agent.py:_is_context_length_error`.
- **What:** Body check looks for `code` field; Anthropic uses `type`.
- **Fix:** also check `body.get("error", {}).get("type") ==
  "invalid_request_error"` paired with a message-substring match.

#### R56. `_seal_tool_batch` no bounds check on `finishing_index`
- **Where:** `taskgenie/agent.py:_seal_tool_batch`.
- **Fix:** defensive assert; raise ValueError on invalid index.

#### R58. `bulk_load` spawn order can corrupt TUI tree on resume
- **Where:** `taskgenie/agent.py:bulk_load`.
- **What:** Dict iteration is insertion-order; if a child happened
  to be saved before its parent, the re-emitted child spawn arrives
  first and TUI sets `root_id` to the child.
- **Fix:** sort by `depth` ascending before re-emitting.

#### R59. `read_workspace_paths` lookup not normalized
- **Where:** `taskgenie/tools/work.py:_read_workspace`,
  `_write_workspace`.
- **What:** Stored as raw `args["path"]`. `read_workspace('game.py')`
  + `write_workspace('./game.py', mode='overwrite')` misses the
  warning because the strings don't match.
- **Fix:** normalize via `os.path.normpath` at both insert and
  lookup.

#### R60. `diff_tools_against` false-positive for pre-ceo-bucket sessions
- **Where:** `taskgenie/session.py:diff_tools_against`.
- **What:** Sessions saved before round-2's ceo bucket addition have
  no `ceo` key in `tool_signature`; the diff treats this as "ceo
  bucket was empty → all current ceo tools are new" and prints a
  bogus `+ ask_user (new since last run)` warning.
- **Fix:** treat missing-bucket as "no signature recorded; skip".

#### R61. Abandoned/error session resumable with no context shown
- **Where:** `taskgenie/__main__.py:_run_resume`.
- **What:** If `status in ("abandoned", "error")` and no live pause
  in post-resolution history, falls through to the generic blocker
  prompt without any indication of why the session ended.
- **Fix:** explicit branch printing what happened, then asking for
  follow-up.

#### R62. Narrow-terminal layout still misbehaves (round-two deferred)
- **Where:** `taskgenie/tui.py:_render`.
- **Fix:** if `console.size.width < 70`, switch from `split_row` to
  `split_column` (stack focus over tree) so both stay readable.

#### R63. `_print_status_panel` REGISTRY check is identity-blind
- **Where:** `taskgenie/__main__.py:_print_status_panel`.
- **Fix:** `if Agent.REGISTRY.get(ceo.id) is not ceo:` (verify
  instance identity, not just id presence).

#### R64. `exec_workspace` schema lies about exposed env vars
- **Where:** `taskgenie/tools/work.py:_EXEC_WORKSPACE_SCHEMA`.
- **What:** Says only `$TASKGENIE_SCRATCH` is exposed; both are.
- **Fix:** correct the asymmetry with the `exec_code` description.

#### R65. CEO can't `exec_workspace` but the prompt doesn't say so
- **Where:** `taskgenie/prompts.py:CEO_ROLE_PROMPT`.
- **Fix:** add a paragraph explaining "you can read but not run
  workspace files; if a runtime check matters, hire a Verifier
  worker".

#### R66. Archivist prompt loses exec-tool distinction in summaries
- **Where:** `taskgenie/archivist.py:ARCHIVIST_SYSTEM_PROMPT`.
- **Fix:** add to the "preserve" bullets that summaries should note
  whether code execution was in scratch (exec_code) or workspace
  (exec_workspace).

#### R67. Worker prompt doesn't explain `[archived summary]` marker
- **Where:** `taskgenie/prompts.py` worker CONTEXT MANAGEMENT block.
- **Fix:** add a one-paragraph note that synthetic user messages
  prefixed with `[archived summary of N earlier messages]` are the
  Archivist's lossy digest, not fresh user input.

### LOW

#### R68. Control chars in paths via `_safe_path`
- **Where:** `taskgenie/tools/work.py:_safe_path`.
- **What:** Embedded `\n` in a path makes success tool-replies
  multi-line — mild prompt-injection. NUL is already rejected.
- **Fix:** extend the rejection to all `< 0x20` and `\x7f`.

#### R69. MCP namespace collision reports only the last error
- **Where:** `taskgenie/mcp_client.py:_register_mcp_tool`.
- **Fix:** accumulate per-server collisions to a list.

#### R71. `EventKind` Literal still missing some emitted kinds
- **Where:** `taskgenie/events.py`.
- **Fix:** add `hire`, `assign`, `review`, `fire`, `rehire` to the
  Literal so type-checkers don't need ignores at the call sites.

#### R73. `_truncate_for_log` measures characters, names bytes
- **Where:** `taskgenie/event_logger.py`.
- **Fix:** rename constant `_MAX_EVENT_FIELD_BYTES` →
  `_MAX_EVENT_FIELD_CHARS`; update message wording for consistency.

#### R74. `_reset_team` bypasses `_set_status` (same pattern R11 fixed)
- **Where:** `taskgenie/__main__.py:_reset_team`.
- **Fix:** `agent._set_status("fired", reason=...)` instead of
  direct `.status = "fired"` + manual `BUS.publish`.

#### R75. Input helpers swallow KeyboardInterrupt
- **Where:** `taskgenie/__main__.py:_read_*_input`.
- **What:** They catch KeyboardInterrupt and return None, so the
  user can't actually Ctrl+C the process at the input prompt.
  Inconsistent with the outer Ctrl+C → snapshot+130 contract.
- **Fix:** re-raise; the outer handler snapshots.

#### R76. `compaction_end` `success=False` shows literal "no-op"
- **Where:** `taskgenie/agent.py:_maybe_compact` (event data).
- **Fix:** always supply a `reason` field; TUI prefers reason over
  "no-op" fallback.

#### R77. R28 ask_user description fix is half-done
- **Where:** `taskgenie/tools/management.py:_ASK_USER_SCHEMA`.
- **Fix:** drop the "subordinates can't see this tool" half (only
  the CEO sees this schema, so it's dead text); keep the useful
  blocker→ask_user conversion explanation.

#### R78. `read_file` / `write_file` schemas don't reinforce privacy
- **Where:** `taskgenie/tools/work.py`.
- **Fix:** add "PRIVATE scratch (no other agent sees this)" framing
  symmetric to `read_workspace`/`write_workspace`.

#### R79. Tool registration order interleaves exec before workspace writes
- **Where:** `taskgenie/tools/work.py:register_builtin_work_tools`.
- **Fix:** reorder to scratch-trio then workspace-quintet so the
  cross-references in descriptions resolve naturally.

### Test gaps to fill in this PR

- R3, R12, R13, R18, R22, R24, R27 — round-two fixes with no test.
- R6 `bypass_semaphore` — never verified.
- R8 persistence round-trip (save → load → still warns).
- `_safe_recent_keep` starvation case.
- `exec_workspace` tool-kind exposure (worker-only).
- MCP namespace collision auto-rename.
- New tests for every round-three CRITICAL.

### Test bugs to fix

- `test_persistence::test_tool_diff_on_resume` re-registers
  `exec_code` with stale `version="0.1"` (real is `0.3`), polluting
  cross-test state.
- `test_review_rt2.test_maybe_compact_repeat_guard` and
  `test_compaction_emits_events_on_target_agent` monkeypatch
  `compact_messages` without try/finally.
- `test_followup_loop` patches `_read_followup_input` without
  saving/restoring.

### Docs

- README mentions neither `exec_workspace` nor the new
  `exec_code`/`exec_workspace` split.
- `.env.example` missing `TASKGENIE_PROJECTS_DIR` and
  `TASKGENIE_EXEC_ENV_PASSTHROUGH`.

---

## Easy-win cleanup pass — done in this PR

Deferred items from rounds 1 + 2 that took <10 LOC each:

- `_safe_jsonable` bool/int key collision (`artifacts.py`) — keys now
  stringified explicitly so `{True: 'a', 'true': 'b'}` no longer
  silently drops one value.
- `SessionData.status` docstring missing `"asking"` (`session.py`).
- `_reset_team` now removes fired agents from `Agent.REGISTRY` so
  `state.json` doesn't grow unbounded over many `/reset`s
  (`__main__.py`).
- `_format_activity_entry` right-justifies labels via `cell_len`, so
  CJK / fullwidth glyphs in future labels don't drift the column
  (`tui.py`).
- `test_tui` uses `io.StringIO()` instead of `open("/dev/null", "w")`
  so the suite runs on Windows.
- Manager prompt now explicitly tells managers to use `report_blocker`
  for user-side clarifications (not `ask_user`, which is CEO-only).
- README "What's in the box" mentions context compaction / Archivist
  for the first time.
- README test list includes `test_review_rt3.py`.
- `.env.example` documents `TASKGENIE_PROJECTS_DIR` and
  `TASKGENIE_EXEC_ENV_PASSTHROUGH`.

---

## Still deferred — surfaced for the next pass

These were known going in or surfaced in round 3 but were intentionally
left out because each is non-trivial OR borderline cosmetic:

### Real-but-bounded (worth a focused PR eventually)

- **Bus queues unbounded.** `events.py:EventBus` subscribers use
  unbounded `asyncio.Queue`. Only bites when a subscriber forgets
  `unsubscribe`. Right answer: bounded queue + drop-oldest when full.
  ~30 LOC + a test.
- **`task_assignments` leak on cancelled child tasks** (orchestrator
  M5 from round 2). Generates "Task was destroyed but pending"
  warnings at shutdown. Cleanup pass in `_tool_fire` and at end of
  `run`.
- **`_kill_proc_tree` pid-reuse race** (theoretical on Linux; the
  parent hasn't reaped). Would need a tighter "wait for child
  briefly, only then killpg" sequence.
- **`console.input` overlap after `Live` exits the alternate screen**
  (presentation 4 from round 2). Tmux/screen-specific cosmetic.
- **`_ColumnOverflowTree` measurement floor of 200 cells** over-truncates
  labels longer than the terminal width but shorter than 200. Edge case.
- **`_unique_project_dir` TOCTOU** across simultaneous CLI invocations.
  Not realistic for a single-user tool.
- **`count_tokens` called 3x on the success compaction path**
  (orchestrator L1 from round 3). Micro-optimisation; the math is cheap.

### Observability gaps

- **`RunResult.to_tool_payload` carries no compaction telemetry**
  (orchestrator L3 from round 3). The parent agent learns nothing
  about how often the child compacted; the data is on the JSONL log
  but not in the structured reply.

### Test coverage gaps still open

- `_tool_review`, `_tool_rehire` have no direct tests (only indirect
  via happy-path scenarios). `_tool_fire` is now tested for state
  emit (round 3) but not for cancellation semantics.
- `bulk_load` parent-relinking has no test for missing-parent or
  absent-child-id cases.
- `result.RunResult.to_tool_payload` has zero direct coverage
  (mentioned as a bright spot in round 1 — should test the kinds).
- MCP namespace collision / `_connect_server` / `_register_mcp_tool`
  still uncovered (only the timeout + close paths are tested).
- Resume of `status="asking"` (user quit mid-question without
  answering) has no test of the resume code path.
- Direct tests for round-two R12 (Ctrl+C → snapshot), R13 (dangling
  ceo_task cleanup), R22 (TUI tool_args truncation explicitly), R24
  (Measurement narrow-width clamp), R27 (`_run_resume` snapshot after
  every mark_*) — only covered implicitly today.

### Doc gaps

- **No `AGENTS.md`.** Acknowledged from round 1; would be a fresh
  doc-only PR.
- **CEO prompt's `FOLLOW-UP TURNS` section duplicates content** that
  `_build_followup_message` writes at runtime. Minor token waste.
- **Status section in README** says all features are "covered by smoke
  tests" — true at a high level, but a reader might overestimate the
  coverage given the gaps above.

### Won't-fix

- **R57: tool-dump pointers can survive into Archivist summaries.**
  The agent might later try `read_file('tool-...')` and find the
  scratch GC has reaped it. Acceptable: scratch lifetime ≠ message
  lifetime, and the worst case is a tool-call error the agent can
  recover from.
- **Test infra: `test_review_prb.py` mutates `CONFIG.data_dir`
  without restore.** Cross-process safe (each test file runs as its
  own `python -m`), only matters if anyone ever runs the suite in a
  single process. Acknowledged-as-known.

---

## Round-two findings — FIXED (history)

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
