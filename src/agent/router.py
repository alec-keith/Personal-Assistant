"""
Roman-Elite v1.1: Orchestrator routing infrastructure.

Loads complexity_matrix.json and router_rules_v2.json at import time.
Provides:
  - build_orchestrator_routing_context() → prompt section for the system prompt
  - route_to_specialists_handler() → the tool handler for the route_to_specialists tool
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from .specialists import (
    call_specialist,
    call_specialists_parallel,
    format_specialist_outputs,
    SPECIALISTS,
)

logger = logging.getLogger(__name__)

FRAMEWORK_DIR = Path(__file__).parent.parent.parent / "Framework"


def _load_json(filename: str) -> dict:
    path = FRAMEWORK_DIR / filename
    try:
        return json.loads(path.read_text())
    except Exception:
        logger.error("Failed to load %s", path)
        return {}


COMPLEXITY_MATRIX = _load_json("complexity_matrix.json")
ROUTER_RULES = _load_json("router_rules_v2.json")


def build_orchestrator_routing_context() -> str:
    """
    Generate the orchestrator routing section for the system prompt.
    Includes the complexity matrix, intent defaults, escalation triggers,
    and specialist add/remove rules — all derived from the JSON configs.
    """
    levels = COMPLEXITY_MATRIX.get("levels", {})
    intent_defaults = COMPLEXITY_MATRIX.get("intent_defaults", [])
    escalation_triggers = COMPLEXITY_MATRIX.get("routing_rules", {}).get("escalation_triggers", [])
    specialist_add_rules = COMPLEXITY_MATRIX.get("routing_rules", {}).get("specialist_add_rules", [])
    cap_policy = COMPLEXITY_MATRIX.get("committee_cap_policy", {})

    # Build the prompt section
    parts = [
        "## Orchestrator Routing Rules",
        "",
        "You are the Roman-Orchestrator. For EVERY message, follow this process:",
        "1. Classify the intent (match to one of the intent_defaults below)",
        "2. Determine the complexity level (0=Atomic, 1=Tactical, 2=Strategic)",
        "3. Check escalation triggers — bump the level if any match",
        "4. Apply specialist_add_rules to include relevant domain specialists",
        "5. For Level 0: handle DIRECTLY with existing tools. Do NOT call route_to_specialists.",
        "6. For Level 1-2: call `route_to_specialists` with intent, level, specialists, tool_pulls, and a context summary.",
        "   Then incorporate the specialist insights into your final response to the user.",
        "",
        "### Complexity Levels",
    ]

    for lvl_id, lvl in sorted(levels.items()):
        parts.append(f"- **Level {lvl_id} ({lvl['name']})**: {lvl['description']} (max {lvl.get('max_specialists', 0)} specialists)")

    parts.append("")
    parts.append("### Intent Defaults")
    parts.append("| Intent | Level | Specialists | Tool Pulls |")
    parts.append("|--------|-------|-------------|------------|")
    for intent in intent_defaults:
        specs = ", ".join(intent.get("default_specialists", [])) or "—"
        tools = ", ".join(intent.get("tool_pulls", [])) or "—"
        examples = intent.get("examples", [])
        ex_str = f" (e.g. \"{examples[0]}\")" if examples else ""
        parts.append(f"| {intent['intent']}{ex_str} | {intent['level']} | {specs} | {tools} |")

    parts.append("")
    parts.append("### Escalation Triggers")
    parts.append("If any of these are detected, bump the complexity level:")
    for trigger in escalation_triggers:
        parts.append(f"- {trigger['trigger']}: {trigger['reason']} → escalate to Level {trigger['escalate_to_level']}")

    parts.append("")
    parts.append("### Specialist Add Rules")
    parts.append("Based on message content, dynamically add specialists:")
    for rule in specialist_add_rules:
        signals = rule.get("if_signal_contains_any", [])
        specs = rule.get("add_specialists", [])
        if signals:
            parts.append(f"- If message contains [{', '.join(signals)}] → add {', '.join(specs)}")
        elif rule.get("if_any_write_tools_planned"):
            parts.append(f"- If any write tools are planned → add {', '.join(specs)}")

    parts.append("")
    parts.append("### Committee Cap Policy")
    parts.append(f"- Level 0: max {cap_policy.get('level_0_cap', 0)} specialists")
    parts.append(f"- Level 1: max {cap_policy.get('level_1_cap', 2)} specialists")
    parts.append(f"- Level 2: max {cap_policy.get('level_2_cap', 7)} specialists")
    parts.append(f"- Priority order: {cap_policy.get('hard_rule', 'Exec > Strategy > Critic > domain-specific > Narrative')}")

    return "\n".join(parts)


async def route_to_specialists_handler(
    client: anthropic.AsyncAnthropic,
    intent: str,
    level: int,
    specialists: list[str],
    tool_pulls: list[str],
    context_summary: str,
    tool_data: dict[str, str] | None = None,
) -> str:
    """
    The handler for the route_to_specialists tool.

    1. Uses pre-gathered tool_data (already collected by the orchestrator's agent loop)
       or builds context from the summary
    2. Calls specialists (parallel for Level 2, sequential for Level 1)
    3. If Narrative is in the list, makes a final narrative pass
    4. Returns formatted results for the Orchestrator

    The Orchestrator passes tool_data when it has already gathered tool results
    (calendar events, tasks, emails, etc.) through normal tool calls.
    """
    logger.info("[SPECIALISTS] Routing — intent=%s level=%d specialists=%s tool_pulls=%s",
                 intent, level, specialists, tool_pulls)

    # Build full context for specialists
    context_parts = [
        f"Intent: {intent}",
        f"Complexity Level: {level}",
        f"User Context: {context_summary}",
    ]

    if tool_data:
        context_parts.append("\n--- Gathered Data ---")
        for tool_key, data in tool_data.items():
            context_parts.append(f"\n[{tool_key}]\n{data}")

    full_context = "\n".join(context_parts)

    # Separate Narrative from analysis specialists
    analysis_specialists = [s for s in specialists if s != "Roman-Narrative"]
    include_narrative = "Roman-Narrative" in specialists

    # Call analysis specialists
    if level >= 2:
        # Strategic: parallel fan-out
        logger.info("[SPECIALISTS] Level 2 parallel fan-out: %s", analysis_specialists)
        outputs = await call_specialists_parallel(client, analysis_specialists, full_context)
    else:
        # Tactical: sequential (1-2 specialists)
        logger.info("[SPECIALISTS] Level 1 sequential: %s", analysis_specialists)
        outputs = []
        accumulated_context = full_context
        for spec_name in analysis_specialists:
            result = await call_specialist(client, spec_name, accumulated_context)
            logger.info("[SPECIALISTS] %s returned — summary=%s", spec_name,
                         (result.get("summary") or "")[:100])
            outputs.append(result)
            # Each subsequent specialist sees prior outputs
            accumulated_context = full_context + "\n\n--- Prior Specialist Outputs ---\n" + format_specialist_outputs(outputs)

    # Format outputs for the Orchestrator
    formatted = format_specialist_outputs(outputs)
    logger.info("[SPECIALISTS] All done — %d specialists responded, %d chars output",
                 len(outputs), len(formatted))

    # If Narrative requested, make a synthesis pass
    if include_narrative and outputs:
        narrative_context = (
            f"Here are the specialist analyses for a {intent} request:\n\n"
            f"{formatted}\n\n"
            f"Original context:\n{context_summary}\n\n"
            f"Synthesize these into a single cohesive response in Roman's voice. "
            f"Plain text for iMessage. Keep it tight. Include the key recommendations "
            f"and any risks worth mentioning. Do not lose any critical action items."
        )
        narrative_result = await call_specialist(
            client, "Roman-Narrative", narrative_context,
            model="claude-sonnet-4-6",  # Use Sonnet for narrative quality
        )
        narrative_text = narrative_result.get("narrative", "")
        if narrative_text:
            formatted += f"\n\n--- Roman-Narrative Synthesis ---\n{narrative_text}"

    # Collect tool requests from all specialists
    all_tool_requests = []
    for out in outputs:
        all_tool_requests.extend(out.get("tool_requests", []))

    if all_tool_requests:
        formatted += "\n\n--- Specialist Tool Requests ---\n"
        formatted += json.dumps(all_tool_requests, indent=2)

    return formatted
