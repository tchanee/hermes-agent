from types import SimpleNamespace

import pytest

from agent.codex_control_context import render_codex_stable_policy


def test_stable_policy_excludes_volatile_memory_and_context():
    agent = SimpleNamespace(
        _build_system_prompt_parts=lambda: {
            "stable": "SOUL POLICY\nActive Hermes profile: orchestrator",
            "context": "PROJECT INSTRUCTIONS",
            "volatile": "USER SECRET\nMEMORY FACT",
        }
    )
    rendered = render_codex_stable_policy(agent)
    assert "SOUL POLICY" in rendered.developer_instructions
    assert "orchestrator" in rendered.developer_instructions
    assert "PROJECT INSTRUCTIONS" not in rendered.developer_instructions
    assert "USER SECRET" not in rendered.developer_instructions
    assert "MEMORY FACT" not in rendered.developer_instructions
    assert rendered.revision.startswith("sha256:")


def test_stable_policy_fails_closed_on_overflow():
    agent = SimpleNamespace(
        _build_system_prompt_parts=lambda: {
            "stable": "x" * 100,
            "context": "",
            "volatile": "",
        }
    )
    with pytest.raises(RuntimeError, match="limit is 32"):
        render_codex_stable_policy(agent, max_bytes=32)


def test_stable_policy_revision_is_deterministic_and_content_bound():
    def render(text):
        agent = SimpleNamespace(
            _build_system_prompt_parts=lambda: {
                "stable": text, "context": "", "volatile": ""
            }
        )
        return render_codex_stable_policy(agent)

    assert render("same").revision == render("same").revision
    assert render("same").revision != render("different").revision
