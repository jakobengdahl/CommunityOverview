from typing import Any, Dict


def get_language_policy(presentation: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized graph language policy from presentation config."""
    policy = dict(presentation.get("language_policy") or {})
    primary_language = policy.get(
        "primary_language", presentation.get("default_language", "en")
    )
    allowed_languages = policy.get("allowed_languages") or [primary_language]

    return {
        "mode": policy.get("mode", "preferred"),
        "primary_language": primary_language,
        "allowed_languages": allowed_languages,
        "description_sv": policy.get("description_sv", ""),
        "description_en": policy.get("description_en", ""),
    }


def format_language_policy_for_prompt(
    presentation: Dict[str, Any], *, external_agent: bool = False
) -> str:
    """Format graph language policy instructions for prompts and MCP agents."""
    policy = get_language_policy(presentation)
    allowed_languages = ", ".join(policy["allowed_languages"])
    description = (
        policy["description_en"]
        or policy["description_sv"]
        or "No explicit language policy has been configured."
    )
    user_language_line = (
        "- You may still respond to the user in their own language, but any new or updated graph content must follow this language policy."
        if external_agent
        else "- You may respond to the user in their own language, but any new or updated graph content must follow this language policy."
    )

    return (
        "LANGUAGE POLICY:\n"
        f"- Mode: {policy['mode']}\n"
        f"- Primary language for graph content: {policy['primary_language']}\n"
        f"- Allowed languages for graph content: {allowed_languages}\n"
        f"- Policy description: {description}\n"
        f"{user_language_line}\n"
    )
