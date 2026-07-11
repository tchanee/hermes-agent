from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.codex_control_context import render_codex_stable_policy


def test_stable_policy_excludes_volatile_memory_and_context():
    agent = SimpleNamespace(
        ephemeral_system_prompt="UNTRUSTED SESSION METADATA",
        _codex_trusted_channel_policy="TOPIC POLICY",
        platform="telegram",
        thread_id="7351",
    )
    with patch("run_agent.load_soul_md", return_value="SOUL POLICY"), patch(
        "agent.file_safety._resolve_active_profile_name", return_value="orchestrator"
    ):
        rendered = render_codex_stable_policy(agent)
    assert "SOUL POLICY" in rendered.developer_instructions
    assert "orchestrator" in rendered.developer_instructions
    assert "TOPIC POLICY" in rendered.developer_instructions
    assert "UNTRUSTED SESSION METADATA" not in rendered.developer_instructions
    assert "7351" in rendered.developer_instructions
    assert "PROJECT INSTRUCTIONS" not in rendered.developer_instructions
    assert "USER SECRET" not in rendered.developer_instructions
    assert "MEMORY FACT" not in rendered.developer_instructions
    assert "(`cronjob`, `kanban`, or `skills`)" in rendered.developer_instructions
    assert "routine detached Hermes worker" in rendered.developer_instructions
    assert rendered.revision.startswith("sha256:")


def test_stable_policy_fails_closed_on_overflow():
    agent = SimpleNamespace(
        _codex_trusted_channel_policy="",
        platform="telegram",
        thread_id="7351",
    )
    with patch("run_agent.load_soul_md", return_value="x" * 100):
        with pytest.raises(RuntimeError, match="limit is 32"):
            render_codex_stable_policy(agent, max_bytes=32)


def test_stable_policy_revision_is_deterministic_and_content_bound():
    def render(text):
        agent = SimpleNamespace(
            _codex_trusted_channel_policy="", platform="telegram", thread_id="7351"
        )
        with patch("run_agent.load_soul_md", return_value=text):
            return render_codex_stable_policy(agent)

    assert render("same").revision == render("same").revision
    assert render("same").revision != render("different").revision


def test_native_stable_prompt_bulk_is_never_rendered():
    agent = SimpleNamespace(
        _codex_trusted_channel_policy="TOPIC POLICY",
        platform="telegram",
        thread_id="7351",
        _build_system_prompt_parts=lambda: (_ for _ in ()).throw(
            AssertionError("native prompt must not be rendered")
        ),
    )
    with patch("run_agent.load_soul_md", return_value="SOUL POLICY"):
        rendered = render_codex_stable_policy(agent)
    assert rendered.size_bytes < 24 * 1024
