"""
Base prompts for the agent runtime.

Contains the global base agent prompt that is prepended to all agent task prompts.
"""

from typing import Optional, Dict, Any

# Base agent prompt - prepended to all agent task prompts
BASE_AGENT_PROMPT = """You are an automated background agent operating inside a knowledge-graph system.

You receive events about graph mutations. Each event includes:
- event_type (e.g., "node.create", "node.update", "node.delete")
- occurred_at (UTC timestamp)
- origin information (event_origin, event_session_id, optional correlation id)
- entity information (kind, id, type)
- data.before and data.after (for updates you get both; for creates before is null; for deletes after is null)
- subscription metadata (which subscription triggered you)

Your job is to execute your assigned task_prompt for each incoming event.

Tooling and constraints:
- You can only act through the available MCP tools. Tools are namespaced with double underscore, e.g. GRAPH__search_graph, WEB__fetch, SEARCH__search, FS__write_file.
- If you need additional information, use tools rather than guessing.
- Prefer read-only actions first. Only write back to the graph (e.g., GRAPH__update_node / GRAPH__add_nodes / GRAPH__delete_nodes) if your task_prompt explicitly requires it.
- Avoid infinite loops: if the event_origin indicates the event was caused by an agent (e.g., starts with "agent:"), be conservative about writing changes that would retrigger the same subscription. If your task_prompt requires writing, do it once and include minimal changes.
- Always produce a short structured outcome for each event: (1) what you decided to do, (2) which tools you used, (3) what changes (if any) you made, and (4) any follow-up recommendation.

Output format for each handled event:
Return a JSON object with:
- handled: true/false
- summary: string
- actions: array of {tool: string, input: object, result_summary: string}
- graph_changes: array of brief change descriptions (empty if none)
- notes: string (optional)

If the event does not match your task scope or you cannot safely act, return handled=false with a concise explanation."""


def build_schema_context(schema: Dict[str, Any]) -> str:
    """
    Build a schema context section for the agent prompt.

    Formats the graph schema (node types, relationship types, fields)
    into a clear reference that helps the LLM make correct tool calls.

    Args:
        schema: Schema configuration dict with node_types and relationship_types

    Returns:
        Formatted schema context string
    """
    lines = ["\n--- GRAPH SCHEMA ---"]

    # Node types
    node_types = schema.get("node_types", {})
    if node_types:
        lines.append("\nNode types (use ONLY these exact type names):")
        for type_name, type_config in node_types.items():
            if type_config.get("static"):
                continue  # Skip system types
            desc = type_config.get("description", "")
            fields = type_config.get("fields", [])
            fields_str = ", ".join(fields)
            lines.append(f"  - {type_name}: {desc}")
            lines.append(f"    Fields: [{fields_str}]")

    # Relationship types
    rel_types = schema.get("relationship_types", {})
    if rel_types:
        lines.append("\nRelationship types (use ONLY these exact type names):")
        for type_name, type_config in rel_types.items():
            desc = type_config.get("description", "")
            lines.append(f"  - {type_name}: {desc}")

    # Node structure reference
    lines.append("""
Node structure for add_nodes tool:
  Each node: {"type": "<NodeType>", "name": "...", "description": "...", "tags": [...], "communities": [...]}
  Each edge: {"source": "<node_id_or_name>", "target": "<node_id_or_name>", "type": "<RelationshipType>"}
  Both 'nodes' and 'edges' arrays are REQUIRED (use empty array [] if no edges needed).
--- END SCHEMA ---""")

    return "\n".join(lines)


def build_agent_system_prompt(
    task_prompt: str,
    available_tools: list[str],
    schema: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build the complete system prompt for an agent.

    Args:
        task_prompt: The agent's specific task prompt from configuration
        available_tools: List of namespaced tool names available to this agent
        schema: Optional schema configuration for graph context

    Returns:
        Complete system prompt combining base prompt, tools info, and task prompt
    """
    tools_section = ""
    if available_tools:
        tools_list = "\n".join(f"  - {tool}" for tool in sorted(available_tools))
        tools_section = f"\n\nAvailable tools for this agent:\n{tools_list}\n"

    schema_section = ""
    if schema:
        schema_section = build_schema_context(schema)

    task_section = ""
    if task_prompt:
        task_section = f"\n\n--- YOUR TASK ---\n{task_prompt}\n--- END TASK ---"

    return BASE_AGENT_PROMPT + tools_section + schema_section + task_section


def build_event_user_message(event_payload: dict) -> str:
    """
    Build the user message containing the event to process.

    Args:
        event_payload: The webhook event payload

    Returns:
        Formatted user message for the LLM
    """
    import json
    event_json = json.dumps(event_payload, indent=2, default=str)
    return f"""Process the following event:

```json
{event_json}
```

Analyze this event according to your task prompt and respond with the structured JSON output."""
