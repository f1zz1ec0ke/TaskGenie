from __future__ import annotations

MANAGEMENT_TOOLS = [
    {
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
    },
    {
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
    },
    {
        "type": "function",
        "function": {
            "name": "await_results",
            "description": (
                "Block until the given task handles complete, then return each subordinate's "
                "final report. Pass multiple handles to gather parallel work."
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
]
