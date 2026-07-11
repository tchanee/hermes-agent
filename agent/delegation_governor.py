"""Hard per-turn execution budget for responsive messaging parents."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_EXEMPT_TOOLS = frozenset({"delegate_task", "clarify", "todo", "memory"})


def _load_policy() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        policy = load_config().get("delegation_governor") or {}
        return policy if isinstance(policy, dict) else {}
    except Exception:
        return {}


def authorize_parent_tool(agent: Any, tool_name: str) -> Optional[str]:
    """Return a blocking message when a messaging parent exceeds its budget."""
    policy = _load_policy()
    if not policy.get("enabled", False):
        return None
    if str(getattr(agent, "platform", "") or "").lower() == "subagent":
        return None

    platforms = policy.get("platforms") or ["telegram"]
    allowed_platforms = {str(item).strip().lower() for item in platforms}
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    if platform not in allowed_platforms:
        return None

    exempt = set(_DEFAULT_EXEMPT_TOOLS)
    configured_exempt = policy.get("exempt_tools")
    if isinstance(configured_exempt, list):
        exempt.update(str(item).strip() for item in configured_exempt)
    if tool_name in exempt:
        return None

    blocked_tools = policy.get("blocked_parent_tools") or []
    if tool_name in {str(item).strip() for item in blocked_tools}:
        logger.info("Delegation governor blocked parent-only tool %s", tool_name)
        return (
            f"PARENT_TOOL_REQUIRES_DELEGATION: {tool_name} is disabled in the "
            "responsive Telegram parent. Dispatch the work immediately with "
            "delegate_task instead. Do not run another Hermes profile through "
            "terminal; delegate_task is the non-blocking worker path."
        )

    turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    state = getattr(agent, "_delegation_governor_state", None)
    if not isinstance(state, dict) or state.get("turn_id") != turn_id:
        state = {"turn_id": turn_id, "parent_tools_used": 0}
        agent._delegation_governor_state = state

    try:
        limit = max(0, int(policy.get("max_parent_tools", 2)))
    except (TypeError, ValueError):
        limit = 2

    if state["parent_tools_used"] >= limit:
        logger.info(
            "Delegation governor blocked parent tool %s after %d/%d calls",
            tool_name,
            state["parent_tools_used"],
            limit,
        )
        return (
            "PARENT_EXECUTION_BUDGET_REACHED: This Telegram parent has used "
            f"its {limit}-tool execution budget for the current turn. Do not "
            f"call {tool_name} or continue investigating here. Dispatch the "
            "remaining work with delegate_task (tier='routine' by default; "
            "tier='important' only for consequential research, trading, or "
            "complex coding), ask a necessary clarification, or answer now."
        )

    state["parent_tools_used"] += 1
    return None
