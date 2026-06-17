# TaskGenie

Task in, working software (or research, or report) out.

A 100%-local hierarchical multi-agent system. A CEO hires managers, who
hire workers (or sub-managers), who do the actual work. Every agent is the
same `Agent` class -- what differs is the tailored role prompt the parent
writes when hiring them.

```
CEO/working                                                          
├── ⠧ EngineManager       manager/working  [3↻ 1✓ workers]          
│   ├── ⠧ PhysicsCoreWorker   implementing collision detection       
│   ├── ⠧ RenderingWorker     write_workspace
│   ├── ⠧ InputHandlerWorker  exec_code
│   └── ● EngineReviewer       done
├── ⠧ LevelDesignManager  manager/working  [2↻ 1! workers]          
│   └── ...
└── ● DocsManager        manager/done    (2 done)                    
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how everything works under
the hood.

## Highlights

- **Recursive hierarchy** -- CEO, manager, worker are one class with different prompts and tools.
- **Parallel departments** -- CEO fans out independent facets to concurrent managers.
- **Required Reviewer pattern** -- Teams hire a Reviewer; manager iterates until it signs off.
- **Shared workspace** -- Workers write real files via `write_workspace`, modify via `edit_workspace` with conflict detection. Files land in `./TaskGenie/<project>/`.
- **Blockers + user questions** -- Agents can `report_blocker` (propagates up) or `ask_user` (CEO only).
- **Session persistence** -- Ctrl+C saves state; `--resume` picks up where you left off.
- **Live TUI** -- Focus+tree view, event log, sarcastic Story Mode narrator, help screen.
- **MCP tools** -- Drop a `mcp.json` and workers get external tools automatically.
- **Context management** -- Archivist compaction + large output dumping keep long runs from blowing the context window.

## Requirements

- Python 3.11+
- An OpenAI-compatible endpoint (LM Studio, vLLM, llama.cpp, etc.) with a tool-calling model loaded.
- `npx` / `uvx` on PATH for the default MCP servers (optional but recommended).

## Install

```bash
pip install -e .
cp .env.example .env             # edit your endpoint and keys
cp mcp.example.json mcp.json     # edit BRAVE_API_KEY or remove servers
```

## Run

```bash
# New task
taskgenie "Build a physics game with slides and ball pits."

# Pin the project directory name
taskgenie "Build a chess engine" --project ChessEngine

# List sessions
taskgenie --list
taskgenie --list --filter "game"

# Resume (latest or by id)
taskgenie --resume
taskgenie --resume 2026-05-25-build-a-physics-game-e9bc

# View stats for a past session
taskgenie --stats <session-id>

# View the agent hierarchy for a past session
taskgenie --tree <session-id>

# Delete a session
taskgenie --delete <session-id>

# Headless mode (CI/scripting)
taskgenie --no-tui "Summarize this codebase"

# JSON output
taskgenie --json "Compare cloud GPU pricing" -o report.html
```

### Interactive follow-ups

When the CEO completes, you stay in the session:

```
> you > make the level harder, double the ball pit count
```

Commands: `/done`, `/status`, `/retry`, `/reset`, `/export`.

### TUI keyboard shortcuts

| key | action |
|---|---|
| `Tab` | Cycle screens (focus+tree, event log, story mode) |
| `1` `2` `3` | Jump to specific screen |
| `?` / `h` | Help |
| `j` / `k` | Cycle focus agent |
| `f` | Toggle fired agents |
| `Ctrl+C` | Save state and exit |

## Where files land

```
./TaskGenie/build-a-physics-game/      ← open this folder
  game.py, entities.py, ...            ← worker-produced deliverables
  README.md                            ← CEO's report

./.taskgenie/sessions/<auto-id>/       ← session metadata
  state.json                           ← for --resume
  events.jsonl                         ← event log
  final.md, final.html, final.json     ← artifacts
```

## Configuration

All config is via `.env`. See [`.env.example`](.env.example) for the
full list with comments. Key vars:

| var | default | meaning |
|---|---|---|
| `TASKGENIE_BASE_URL` | `http://localhost:1234/v1` | OpenAI-compatible endpoint |
| `TASKGENIE_API_KEY` | `lm-studio` | Bearer token (falls back to `OPENAI_API_KEY`) |
| `TASKGENIE_MODEL` | (auto-detect) | Model name (empty = first model at endpoint) |
| `TASKGENIE_CEO_MODEL` | (none) | Override model for CEO role |
| `TASKGENIE_MANAGER_MODEL` | (none) | Override model for managers |
| `TASKGENIE_WORKER_MODEL` | (none) | Override model for workers |
| `TASKGENIE_MAX_DEPTH` | `8` | Max hierarchy depth |
| `TASKGENIE_MAX_TURNS` | `1000` | Max LLM turns per agent |
| `TASKGENIE_MAX_CONCURRENT_LLM` | `2` | Concurrent LLM calls cap |

## Safety

`exec_code` / `exec_workspace` run real subprocesses with a timeout
and env scrub but are **not a security sandbox**. Run this in a VM.

## Status

Everything listed above is implemented and tested. See
[ARCHITECTURE.md](ARCHITECTURE.md) for internals.

**Known limitations**: Bus queues drop oldest on overflow; MCP uses
stdio transport only; the tree pane caps at 10 workers/managers per
parent with overflow indicators.
