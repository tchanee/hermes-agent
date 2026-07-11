from types import SimpleNamespace

from agent import delegation_governor as governor


def _agent(platform="telegram", turn_id="turn-1"):
    return SimpleNamespace(platform=platform, _current_turn_id=turn_id)


def test_parent_is_blocked_after_configured_tool_budget(monkeypatch):
    monkeypatch.setattr(
        governor,
        "_load_policy",
        lambda: {"enabled": True, "platforms": ["telegram"], "max_parent_tools": 2},
    )
    agent = _agent()

    assert governor.authorize_parent_tool(agent, "search_files") is None
    assert governor.authorize_parent_tool(agent, "read_file") is None
    blocked = governor.authorize_parent_tool(agent, "terminal")

    assert blocked is not None
    assert "PARENT_EXECUTION_BUDGET_REACHED" in blocked
    assert "delegate_task" in blocked


def test_delegate_and_clarify_remain_available_after_budget(monkeypatch):
    monkeypatch.setattr(
        governor,
        "_load_policy",
        lambda: {"enabled": True, "platforms": ["telegram"], "max_parent_tools": 0},
    )
    agent = _agent()

    assert governor.authorize_parent_tool(agent, "delegate_task") is None
    assert governor.authorize_parent_tool(agent, "clarify") is None
    assert governor.authorize_parent_tool(agent, "terminal") is not None


def test_budget_resets_for_each_turn(monkeypatch):
    monkeypatch.setattr(
        governor,
        "_load_policy",
        lambda: {"enabled": True, "platforms": ["telegram"], "max_parent_tools": 1},
    )
    agent = _agent()

    assert governor.authorize_parent_tool(agent, "read_file") is None
    assert governor.authorize_parent_tool(agent, "read_file") is not None
    agent._current_turn_id = "turn-2"
    assert governor.authorize_parent_tool(agent, "read_file") is None


def test_subagents_and_other_platforms_are_not_limited(monkeypatch):
    monkeypatch.setattr(
        governor,
        "_load_policy",
        lambda: {"enabled": True, "platforms": ["telegram"], "max_parent_tools": 0},
    )

    assert governor.authorize_parent_tool(_agent("subagent"), "terminal") is None
    assert governor.authorize_parent_tool(_agent("cli"), "terminal") is None


def test_blocking_parent_tools_must_delegate_even_before_budget(monkeypatch):
    monkeypatch.setattr(
        governor,
        "_load_policy",
        lambda: {
            "enabled": True,
            "platforms": ["telegram"],
            "max_parent_tools": 2,
            "blocked_parent_tools": ["terminal", "patch"],
        },
    )
    agent = _agent()

    blocked = governor.authorize_parent_tool(agent, "terminal")

    assert blocked is not None
    assert "PARENT_TOOL_REQUIRES_DELEGATION" in blocked
    assert "delegate_task" in blocked
    assert governor.authorize_parent_tool(agent, "read_file") is None
