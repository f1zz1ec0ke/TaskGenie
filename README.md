# TaskGenie

Task in, result out.

TaskGenie is a 100%-local hierarchical multi-agent system. A top-level CEO
hires managers who hire workers who do the actual work. "Hiring" means
writing a tailored role prompt; "firing" means terminating the subordinate;
"rehiring" means giving them a new charter without losing their identity.

```
CEO
├── ResearchManager (manager)
│   ├── BraveResearcher (worker)
│   ├── DocsResearcher (worker)
│   └── ComparisonAnalyst (worker)
└── ReviewManager (manager)
    └── ReportEditor (worker)
```

## What's in the box

- **Recursive Agent primitive.** CEO, manager, and worker are the same class
  with different prompts and tool sets. The whole hierarchy collapses to one
  loop that calls itself.
- **Async parallel siblings.** A manager fan-outs by calling `assign` N times,
  then one `await_results` gathers them. Each level chooses its own fan-out.
- **First-class blockers.** Any agent can call `report_blocker(reason, needs,
  partial_progress)`. The blocker propagates up; each level can resolve
  locally (rehire / re-assign / change scope) or escalate. CEO blockers
  surface to you with a prompt — your guidance is appended and the CEO
  resumes with full message history intact.
- **Session persistence.** Quit during a blocker, change code (implement a
  tool, add an MCP server, fix a bug), and `taskgenie --resume <id>` — the
  full agent tree is rehydrated, the CEO is told which tools have changed,
  and work continues. `taskgenie --list` shows all sessions.
- **Live TUI.** Three-pane layout: org chart with status spinners and
  spawn-flash for new agents, a focus pane that auto-switches to whichever
  agent just acted, and a scrolling event log.
- **MCP client built in.** Drop a standard `mcp.json` (Claude Desktop /
  Cursor / LM Studio format) at the project root and TaskGenie spawns each
  server as a subprocess and surfaces its tools to workers automatically.
- **Real `exec_code`.** Subprocess with cwd-scoped scratch dir, scrubbed env,
  configurable timeout. Not a security sandbox — Docker isolation will land
  as an opt-in backend.
- **Event log + run summary.** Every event streams to
  `.taskgenie/sessions/<id>/events.jsonl`; after each run a deterministic
  stats panel shows agent counts, blockers, hires/fires, tool-call totals,
  wall time — complemented by the CEO's qualitative "Process retrospective"
  section in the final report.

## Requirements

- Python 3.11+
- An OpenAI-compatible local endpoint (LM Studio, vLLM, llama.cpp server, etc.)
  with a tool-calling-capable model loaded.
- (optional) `npx` / `uvx` available on PATH if you want the example MCP
  servers (Brave search via Node, Fetch via Python).

## Install

```bash
pip install -e .
cp .env.example .env             # then edit your endpoint and keys
cp mcp.example.json mcp.json     # then edit BRAVE_API_KEY (or remove servers)
```

## Run

```bash
# new task
taskgenie "Research coding agents and tell me the pros and cons of each."

# list sessions
taskgenie --list

# resume a specific session (or omit the id for the latest)
taskgenie --resume 2026-05-25-research-coding-agents-a3f1
taskgenie --resume
```

## Configuration (`.env`)

| var | default | meaning |
|---|---|---|
| `TASKGENIE_BASE_URL` | `http://100.84.115.121:1234/v1` | OpenAI-compatible endpoint |
| `TASKGENIE_API_KEY`  | `lm-studio` | Sent as bearer; LM Studio ignores it |
| `TASKGENIE_MODEL`    | (auto-detect) | If empty, picks the first model the endpoint reports |
| `TASKGENIE_TEMPERATURE` | `0.4` | LLM sampling temperature |
| `TASKGENIE_MAX_DEPTH` | `4` | Maximum hierarchy depth (CEO is depth 0) |
| `TASKGENIE_MAX_TURNS` | `25` | Maximum LLM turns per agent |
| `TASKGENIE_MAX_CONCURRENT_LLM` | `4` | Concurrent in-flight LLM calls cap |
| `TASKGENIE_EXEC_TIMEOUT` | `30` | `exec_code` wall-clock timeout (seconds) |
| `TASKGENIE_EXEC_ENV_PASSTHROUGH` | (none) | Extra env vars to expose to `exec_code` |
| `TASKGENIE_DATA_DIR` | `.taskgenie` | Sessions, scratch, and event logs root |
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

## Layout

```
taskgenie/
  agent.py          # the recursive Agent primitive + tool dispatcher
  llm.py            # async OpenAI-compat client (retry, semaphore)
  events.py         # in-process event bus (subscribe/unsubscribe)
  prompts.py        # CEO / manager / worker role-prompt templates
  config.py         # .env-driven config
  session.py        # save / load / list / tool-diff persistence
  event_logger.py   # background JSONL writer for events
  stats.py          # deterministic run-summary computation + panel
  mcp_client.py     # spawn MCP server subprocesses; register tools
  tui.py            # Rich 3-pane live UI (tree, focus, log)
  tools/
    registry.py     # central tool registration + dispatch
    management.py   # schemas for hire/assign/await/review/...
    work.py         # schemas + dispatchers for read/write/exec
  __main__.py       # CLI (new / --resume / --list)

tests/
  test_loop.py         # happy path + full escalation chain
  test_persistence.py  # save/load/resume + tool-diff
```

## Status

- All listed features above are implemented and covered by smoke tests.
- `exec_code` runs real subprocesses with a timeout but isn't a security
  sandbox; Docker-backed isolation will land later as an opt-in backend.
- The MCP client uses stdio transport only (no HTTP MCP yet).
- Per-role LLM temperatures are not yet supported — use whatever your
  model lab recommends as a single value.
