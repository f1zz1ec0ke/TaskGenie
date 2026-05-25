# TaskGenie

Task in, result out.

TaskGenie is an orchestration system where an AI agent "manager" creates several
roles and agents in order to achieve any given task. The mechanic is recursive:
the CEO hires department managers, department managers hire team managers, team
managers hire workers. "Hiring" means writing a tailored role prompt for the new
agent. "Firing" means terminating them; "rehiring" means replacing their charter.

```
CEO
├── ResearchManager (manager)
│   ├── BraveResearcher (worker)
│   ├── DocsResearcher (worker)
│   └── ComparisonAnalyst (worker)
└── ReviewManager (manager)
    └── ReportEditor (worker)
```

## How it works

- One `Agent` class is the only primitive. CEO, manager, worker are all the same
  code with different role prompts and different tool sets.
- Managers get hierarchy tools: `hire`, `assign`, `await_results`, `review`,
  `rehire`, `fire`, `complete`. Workers get work tools: `read_file`,
  `write_file`, `exec_code`, `complete` (plus any MCP-provided tools that the
  LM Studio runtime advertises natively, e.g. Brave search, fetch).
- Concurrency is implicit: a manager calls `assign` N times to dispatch N
  subordinates, then one `await_results` to gather them. Each level decides its
  own fan-out.
- A live tree in the terminal shows the org chart growing, each agent's
  narration, and the latest tool call per node. An event log under the tree
  shows decisions in order.
- **Blockers are first-class.** Any agent (worker or manager) can call
  `report_blocker(reason, needs, partial_progress)` instead of `complete`. The
  parent receives a structured `{"status": "blocked", ...}` from
  `await_results` and can choose to: rehire / fire-and-hire a different
  subordinate, re-assign with new context, change scope — or escalate by
  calling `report_blocker` themselves. If the CEO escalates, the main loop
  pauses, surfaces the blocker, and prompts you for guidance. Your reply is
  appended to the CEO's message history and the loop resumes — prior
  subordinates and their state are still available, so the CEO can re-engage
  them with the new info.

## Requirements

- Python 3.11+
- An OpenAI-compatible local endpoint (LM Studio, vLLM, llama.cpp server, etc.)
  with a tool-calling-capable model loaded.

## Install

```bash
pip install -e .
```

## Run

```bash
# point at your local endpoint (defaults shown)
export TASKGENIE_BASE_URL=http://100.84.115.121:1234/v1
export TASKGENIE_API_KEY=lm-studio

taskgenie "Research coding agents and tell me the pros and cons of each."
```

Configuration via env vars:

| var | default | meaning |
|---|---|---|
| `TASKGENIE_BASE_URL` | `http://100.84.115.121:1234/v1` | OpenAI-compatible endpoint |
| `TASKGENIE_API_KEY`  | `lm-studio` | Sent as bearer; LM Studio ignores it |
| `TASKGENIE_MODEL`    | (auto-detect) | If empty, picks the first model the endpoint reports |
| `TASKGENIE_TEMPERATURE` | `0.4` | LLM sampling temperature |
| `TASKGENIE_MAX_DEPTH` | `4` | Maximum hierarchy depth (CEO is depth 0) |
| `TASKGENIE_MAX_TURNS` | `25` | Maximum LLM turns per agent before timing out |
| `TASKGENIE_SCRATCH`  | `.scratch` | Where workers' file I/O lives |

## v1 status / known stubs

- `exec_code` is stubbed — returns a placeholder so the demo runs. Needs proper
  sandboxing (subprocess + timeout, or e2b/Docker) before being trusted.
- Brave search and URL fetch are assumed to come natively from LM Studio's MCP
  bridge. If they don't, workers will say so in their reports — we can add a
  Python MCP client in pass 2.
- No persistence of agent state across runs.
- No retry/backoff on LLM errors yet.
- The TUI re-renders the whole tree on each event — fine for tens of agents,
  may flicker for hundreds.

## Layout

```
taskgenie/
  agent.py          # the recursive Agent primitive + tool dispatcher
  llm.py            # async LM Studio client
  events.py         # in-process event bus
  prompts.py        # CEO / manager / worker role-prompt templates
  config.py         # env-var driven config
  tui.py            # Rich live tree + event log
  tools/
    management.py   # OpenAI tool schemas for hire/assign/await/review/...
    work.py         # OpenAI tool schemas + impls for read/write/exec
  __main__.py       # entry point
```
