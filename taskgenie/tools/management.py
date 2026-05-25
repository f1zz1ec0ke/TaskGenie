"""Schemas for tools used by managers and the CEO.

These are dispatched directly by Agent._execute_tool / Agent.run since they
mutate agent state and the run loop. We still register them so the registry
can track versions/hashes for resume-time diffs.
"""
from __future__ import annotations

from taskgenie.tools.registry import REGISTRY

_HIRE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "hire",
        "description": (
            "Hire a new subordinate by writing their role prompt. Returns the "
            "new agent_id you will use to assign tasks to them. Does NOT assign "
            "a task yet — call `assign` afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "A descriptive name for the subordinate (PascalCase, e.g. 'CodingAgentResearcher').",
                },
                "role_type": {
                    "type": "string",
                    "enum": ["manager", "worker"],
                    "description": "Manager = can hire others. Worker = does the actual work with tools.",
                },
                "role_prompt": {
                    "type": "string",
                    "description": (
                        "The charter you are writing for this agent: their domain, what to deliver, "
                        "constraints, what tools they should use. Be specific and tailored to the task."
                    ),
                },
            },
            "required": ["name", "role_type", "role_prompt"],
        },
    },
}

_ASSIGN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "assign",
        "description": (
            "Dispatch a task to a subordinate. Returns a task_handle immediately — "
            "the subordinate runs in the background. Call this multiple times before "
            "`await_results` to run subordinates IN PARALLEL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "ID returned by `hire`."},
                "task": {
                    "type": "string",
                    "description": "The specific task you want this subordinate to complete.",
                },
            },
            "required": ["agent_id", "task"],
        },
    },
}

_AWAIT_RESULTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "await_results",
        "description": (
            "Block until the given task handles complete, then return each subordinate's "
            "structured result: {status: 'done'|'blocked'|'error', ...}. Pass multiple "
            "handles to gather parallel work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Handles returned by `assign`.",
                }
            },
            "required": ["task_handles"],
        },
    },
}

_REVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "review",
        "description": (
            "Record your review of a completed task. Use `accepted=false` with feedback "
            "if you intend to re-assign, rehire, or fire. Returns nothing actionable — "
            "purely for narration & audit."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_handle": {"type": "string"},
                "accepted": {"type": "boolean"},
                "feedback": {"type": "string"},
            },
            "required": ["task_handle", "accepted", "feedback"],
        },
    },
}

_REHIRE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rehire",
        "description": (
            "Replace a subordinate's role prompt — same agent identity, new charter. "
            "Resets their message history. Use this when they're capable but mis-scoped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "new_role_prompt": {"type": "string"},
            },
            "required": ["agent_id", "new_role_prompt"],
        },
    },
}

_FIRE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fire",
        "description": (
            "Terminate a subordinate. Cancels their pending tasks. Use when they "
            "are unrecoverable or no longer needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["agent_id", "reason"],
        },
    },
}

_COMPLETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "complete",
        "description": (
            "Return your final report to whoever assigned you the task. Ends your run. "
            "Make the report self-contained — your parent will not see your internal narration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": "The synthesized deliverable. Be thorough and well-structured.",
                }
            },
            "required": ["report"],
        },
    },
}

_REPORT_BLOCKER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "report_blocker",
        "description": (
            "Declare that you cannot complete your task and need help to proceed. Ends your run. "
            "The blocker bubbles up to whoever assigned you. Your parent will either resolve it "
            "(by re-scoping, rehiring, or hiring different subordinates) or escalate it further. "
            "If the CEO calls this, the user is prompted to provide whatever is needed and your "
            "work then resumes. Use this when you've genuinely exhausted your options — not for "
            "minor friction. Do NOT fabricate results to avoid reporting a blocker."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "What is blocking you? Be specific.",
                },
                "needs": {
                    "type": "string",
                    "description": "What specifically would unblock you?",
                },
                "partial_progress": {
                    "type": "string",
                    "description": "What you accomplished before getting stuck.",
                },
            },
            "required": ["reason", "needs"],
        },
    },
}

_SUGGEST_FOLLOW_UP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "suggest_follow_up",
        "description": (
            "Buffer a suggestion for additional work that should be done. Does NOT end your "
            "run — you keep working after calling this. When you eventually `complete`, your "
            "buffered suggestions ride along to your parent in the follow_ups field. The "
            "parent decides whether to spawn a new sibling, expand their own scope, or relay "
            "the suggestion further up the chain. Use this when you notice work that's "
            "ADJACENT to your assigned task but out of your direct scope — genuine gaps, "
            "not vague ideas."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "what_is_needed": {
                    "type": "string",
                    "description": "Specifically what additional work should be done.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this matters — what gap or opportunity you noticed.",
                },
                "who_should_do_it": {
                    "type": "string",
                    "description": "Optional hint about who should pick this up (e.g. 'a sub-manager focused on X', 'a worker who can do Y').",
                },
            },
            "required": ["what_is_needed", "rationale"],
        },
    },
}


def register_management_tools() -> None:
    for schema in [
        _HIRE_SCHEMA,
        _ASSIGN_SCHEMA,
        _AWAIT_RESULTS_SCHEMA,
        _REVIEW_SCHEMA,
        _REHIRE_SCHEMA,
        _FIRE_SCHEMA,
    ]:
        REGISTRY.register(schema, kind="management", version="1.0")
    REGISTRY.register(_COMPLETE_SCHEMA, kind="both", version="1.0")
    REGISTRY.register(_REPORT_BLOCKER_SCHEMA, kind="both", version="1.0")
    REGISTRY.register(_SUGGEST_FOLLOW_UP_SCHEMA, kind="both", version="1.0")


register_management_tools()
