from __future__ import annotations

import json

from auto_foundry_core.agent_lifecycle import normalize_codex_json_line


def test_collaboration_states_become_safe_agent_lifecycle() -> None:
    root, rows = normalize_codex_json_line(
        json.dumps({"type": "thread.started", "thread_id": "root-thread"}),
        root_thread=None,
        root_invocation_id="top-level-invocation",
    )
    assert root == "root-thread"
    assert rows == []

    root, rows = normalize_codex_json_line(
        json.dumps({
            "type": "item.updated",
            "item": {
                "id": "tool-1",
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "sender_thread_id": "root-thread",
                "receiver_thread_ids": ["child-1"],
                "prompt": "PRIVATE PROMPT",
                "agents_states": {"child-1": {"status": "running", "message": "PRIVATE RESPONSE"}},
            },
        }),
        root_thread=root,
        root_invocation_id="top-level-invocation",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "agent_progress"
    assert rows[0]["invocation_id"] == "child-1"
    assert rows[0]["parent_agent_id"] == "top-level-invocation"
    assert "PRIVATE" not in json.dumps(rows)


def test_unknown_or_oversized_codex_events_fail_closed() -> None:
    root, rows = normalize_codex_json_line(
        json.dumps({"type": "item.started", "item": {"type": "agent_message", "text": "secret"}}),
        root_thread=None,
        root_invocation_id="top",
    )
    assert root is None
    assert rows == []

    root, rows = normalize_codex_json_line(
        b"{" + b"x" * (65 * 1024) + b"}",
        root_thread=None,
        root_invocation_id="top",
    )
    assert root is None
    assert rows == []
