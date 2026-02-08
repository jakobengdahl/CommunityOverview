"""
Base prompts for the agent runtime.

Contains the global base agent prompt that is prepended to all agent task prompts.
"""

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
- You can only act through the available MCP tools. Tools are namespaced by integration id, e.g. GRAPH.search_graph, WEB.fetch, SEARCH.search, FS.write_file, MAIL.send_email.
- If you need additional information, use tools rather than guessing.
- Prefer read-only actions first. Only write back to the graph (e.g., GRAPH.update_node / GRAPH.add_nodes / GRAPH.delete_nodes) if your task_prompt explicitly requires it.
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


def build_agent_system_prompt(task_prompt: str, available_tools: list[str]) -> str:
    """
    Build the complete system prompt for an agent.

    Args:
        task_prompt: The agent's specific task prompt from configuration
        available_tools: List of namespaced tool names available to this agent

    Returns:
        Complete system prompt combining base prompt, tools info, and task prompt
    """
    tools_section = ""
    if available_tools:
        tools_list = "\n".join(f"  - {tool}" for tool in sorted(available_tools))
        tools_section = f"\n\nAvailable tools for this agent:\n{tools_list}\n"

    task_section = ""
    if task_prompt:
        task_section = f"\n\n--- YOUR TASK ---\n{task_prompt}\n--- END TASK ---"

    return BASE_AGENT_PROMPT + tools_section + task_section


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
