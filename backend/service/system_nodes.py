"""Validated system-node creation shared by MCP and the built-in assistant."""

from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from backend.core.events.delivery import is_safe_url
from backend.core.session_annotations import saved_view_annotation_error
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_MUTATE, GRAPH_OPERATION_STAGE

from . import access
from .staging import refusal_result


class CreateInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, strict=True, allow_inf_nan=False
    )


class NamedInput(CreateInput):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    summary: str = Field(default="", max_length=300)
    tags: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)


class TargetInput(CreateInput):
    entity_kind: Literal["node", "edge"] = "node"
    node_types: List[str] = Field(default_factory=list)
    relationship_types: List[str] = Field(default_factory=list)


class KeywordInput(CreateInput):
    any: List[str] = Field(default_factory=list)


class FederationInput(CreateInput):
    scope: Literal["local_only", "local_and_federated"] = "local_only"
    include_graph_ids: List[str] = Field(default_factory=list)
    max_distance: Optional[int] = Field(default=None, ge=0)


class FiltersInput(CreateInput):
    target: TargetInput = Field(default_factory=TargetInput)
    operations: List[Literal["create", "update", "delete"]] = Field(
        default_factory=lambda: ["create", "update", "delete"], min_length=1
    )
    keywords: KeywordInput = Field(default_factory=KeywordInput)
    federation: FederationInput = Field(default_factory=FederationInput)


class AgentInput(NamedInput):
    task_prompt: str = Field(min_length=1)
    enabled: bool = True
    filters: FiltersInput = Field(default_factory=FiltersInput)
    mcp_integration_ids: List[str] = Field(default_factory=lambda: ["GRAPH"])
    tool_allowlist: Optional[List[str]] = None
    autonomy_level: Literal["observe", "assist", "propose", "act_after_approval"] = (
        "act_after_approval"
    )


class EventSubscriptionInput(NamedInput):
    webhook_url: str = Field(min_length=1, description="Public HTTP(S) webhook URL")
    filters: FiltersInput = Field(default_factory=FiltersInput)
    ignore_origins: List[str] = Field(default_factory=list)
    ignore_session_ids: List[str] = Field(default_factory=list)


class SkillInput(NamedInput):
    content: str = Field(min_length=1)
    when_to_use: str = Field(min_length=1)
    allowed_tools: List[str] = Field(default_factory=list)
    source_url: str = ""
    version: str = ""
    effort: str = ""


class NodePermissions(CreateInput):
    create: bool = False
    update: bool = False
    delete: bool = False


class ActiveKnowledgeCollectionInput(NamedInput):
    short_name: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,98}[a-z0-9])?$")
    introduction_text: str = ""
    prompt: str = ""
    link_results: bool = True
    node_type_permissions: Dict[str, NodePermissions] = Field(default_factory=dict)
    tool_allowlist: List[str] = Field(default_factory=list)


class PositionInput(CreateInput):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class SavedViewInput(NamedInput):
    node_ids: List[str] = Field(min_length=1)
    positions: Dict[str, PositionInput] = Field(default_factory=dict)
    hidden_nodes: List[str] = Field(default_factory=list)
    annotation_document: Optional[Dict[str, Any]] = None


class AnswerInput(CreateInput):
    field_id: str = Field(min_length=1)
    value: JsonValue
    label: Optional[str] = None
    type: Optional[str] = None


class CollectionResponseInput(CreateInput):
    collection_id: str = Field(min_length=1)
    answers: List[AnswerInput] = Field(min_length=1)
    respondent_label: Optional[str] = None
    form_title: Optional[str] = None


SYSTEM_CREATE_MODELS = {
    "create_agent": AgentInput,
    "create_event_subscription": EventSubscriptionInput,
    "create_skill": SkillInput,
    "create_active_knowledge_collection": ActiveKnowledgeCollectionInput,
    "create_saved_view": SavedViewInput,
    "create_collection_response": CollectionResponseInput,
}


def _assemble(config, storage, graph_access):
    """Resolve references before writing anything; all edges use exact IDs."""
    node_id = str(uuid4())
    edges = []
    common = config.model_dump(include=set(NamedInput.model_fields))

    def visible(node_id, node_type=None):
        node = storage.get_node(node_id)
        if (
            node is None
            or not access.is_node_visible(node, graph_access)
            or (node_type and node.type_str != node_type)
        ):
            raise ValueError("Referenced node is unavailable or has the wrong type")
        return node

    if isinstance(config, AgentInput):
        subscription_id = str(uuid4())
        subscription = {
            "id": subscription_id,
            "type": "EventSubscription",
            "name": f"{config.name[:185]} - Subscription",
            "metadata": {
                "filters": config.filters.model_dump(),
                "delivery": {
                    "webhook_url": f"internal://agent/{node_id}",
                    "ignore_origins": [f"agent:{node_id}"],
                    "ignore_session_ids": [],
                },
            },
        }
        metadata = {
            "enabled": config.enabled,
            "subscription_id": subscription_id,
            "prompts": {"task_prompt": config.task_prompt},
            "mcp_integration_ids": config.mcp_integration_ids,
            "tool_allowlist": config.tool_allowlist,
            "autonomy_level": config.autonomy_level,
        }
        agent = {"id": node_id, "type": "Agent", **common, "metadata": metadata}
        # The subscription must already exist when the Agent create event starts it.
        return (
            [subscription, agent],
            [{"source": node_id, "target": subscription_id, "type": "RELATES_TO"}],
            node_id,
        )

    if isinstance(config, EventSubscriptionInput):
        parsed = urlsplit(config.webhook_url)
        # Accessing port also validates malformed/out-of-range port values.
        parsed.port
        if parsed.username or parsed.password or not is_safe_url(config.webhook_url):
            raise ValueError(
                "webhook_url must be a public HTTP(S) URL; use create_agent for internal delivery"
            )
        node_type = "EventSubscription"
        metadata = {
            "filters": config.filters.model_dump(),
            "delivery": config.model_dump(
                include={"webhook_url", "ignore_origins", "ignore_session_ids"}
            ),
        }
    elif isinstance(config, SkillInput):
        node_type = "Skill"
        metadata = config.model_dump(exclude=set(NamedInput.model_fields))
    elif isinstance(config, ActiveKnowledgeCollectionInput):
        node_type = "ActiveKnowledgeCollection"
        if any(
            n.type_str == node_type
            and n.metadata.get("short_name") == config.short_name
            for n in storage.nodes.values()
        ):
            raise ValueError("short_name is already in use")
        # A stable id also makes concurrent creates on shared stores and staged
        # merges conflict rather than publishing two kiosks under the same slug.
        node_id = f"active-knowledge-collection-{config.short_name}"
        metadata = config.model_dump(exclude=set(NamedInput.model_fields))
    elif isinstance(config, SavedViewInput):
        node_type = "SavedView"
        ids = list(dict.fromkeys(config.node_ids))
        for nid in ids:
            visible(nid)
        if set(config.positions) - set(ids) or set(config.hidden_nodes) - set(ids):
            raise ValueError("positions and hidden_nodes must reference node_ids")
        metadata = {
            "node_ids": ids,
            "positions": {
                nid: config.positions[nid].model_dump()
                if nid in config.positions
                else {"x": i * 250, "y": 0}
                for i, nid in enumerate(ids)
            },
            "hidden_nodes": config.hidden_nodes,
        }
        if config.annotation_document is not None:
            metadata.update(
                annotation_schema_version=1,
                annotation_document=config.annotation_document,
            )
        error = saved_view_annotation_error(metadata)
        if error:
            raise ValueError(error)
    else:
        node_type = "CollectionResponse"
        collection = visible(config.collection_id, "ActiveKnowledgeCollection")
        short_name = collection.metadata.get("short_name")
        if not isinstance(short_name, str) or not short_name.strip():
            raise ValueError("Collection has no short_name")
        submitted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        common = {"name": f"{collection.name} — response {submitted_at}"[:200]}
        metadata = {
            **config.model_dump(),
            "collection_short_name": short_name,
            "submitted_at": submitted_at,
        }
        edges = [{"source": node_id, "target": collection.id, "type": "RELATES_TO"}]
    return (
        [{"id": node_id, "type": node_type, **common, "metadata": metadata}],
        edges,
        node_id,
    )


def _create(
    service,
    model,
    config,
    visualization_session_id,
    expected_session_seq,
    *,
    event_origin,
):
    def run(staging):
        # Exactly the authorization action, target and staging operation used by
        # the GUI's add_nodes path, before validation or reference lookups.
        decision = access.evaluate_graph_access(
            service._authorization_hook,
            action=GRAPH_ACTION_MUTATE,
            target="add_nodes",
            graph_operation=GRAPH_OPERATION_STAGE if staging is not None else "",
        )
        if not decision.allowed:
            return access.build_access_denied_result(
                action=GRAPH_ACTION_MUTATE, target="add_nodes", decision=decision
            )
        try:
            parsed = model.model_validate(config)
            with service._storage.write_lock:
                storage = service._storage
                if isinstance(parsed, ActiveKnowledgeCollectionInput) and any(
                    n.type_str == "ActiveKnowledgeCollection"
                    and n.metadata.get("short_name") == parsed.short_name
                    for n in storage.nodes.values()
                ):
                    raise ValueError("short_name is already in use")
                if staging is not None:
                    storage = service.session_gate.compose(
                        staging.session_id, decision.graph_access
                    )
                nodes, edges, node_id = _assemble(
                    parsed, storage, decision.graph_access
                )
                result = service.add_nodes(
                    nodes=nodes,
                    edges=edges,
                    expect_absent=True,
                    expected_session_seq=expected_session_seq,
                    event_origin=event_origin,
                )
                if result.get("success"):
                    result["created_node_id"] = node_id
                return result
        except (ValidationError, ValueError, OSError) as exc:
            return {"success": False, "error": "validation_error", "message": str(exc)}

    try:
        if visualization_session_id == "":
            raise session_context.not_found(visualization_session_id)
        session = session_context.reconcile(
            session_context.current_session_id(), visualization_session_id
        )
        with session_context.bind_session(session):
            return service._write("add_nodes", expected_session_seq, run)
    except Exception as exc:
        refused = refusal_result(exc)
        if refused is None:
            raise
        return refused


def make_system_create_tools(service, *, event_origin=None):
    create = partial(_create, service, event_origin=event_origin)

    def create_agent(
        config: AgentInput,
        visualization_session_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ):
        """Create an Agent (enabled by default) and its linked internal event subscription."""
        return create(
            AgentInput, config, visualization_session_id, expected_session_seq
        )

    def create_event_subscription(
        config: EventSubscriptionInput,
        visualization_session_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ):
        """Create an event subscription delivering to a validated public HTTP(S) webhook."""
        return create(
            EventSubscriptionInput,
            config,
            visualization_session_id,
            expected_session_seq,
        )

    def create_skill(
        config: SkillInput,
        visualization_session_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ):
        """Create a reusable Skill with content and instructions for when to use it."""
        return create(
            SkillInput, config, visualization_session_id, expected_session_seq
        )

    def create_active_knowledge_collection(
        config: ActiveKnowledgeCollectionInput,
        visualization_session_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ):
        """Create a knowledge collection with a unique kiosk short_name and permissions."""
        return create(
            ActiveKnowledgeCollectionInput,
            config,
            visualization_session_id,
            expected_session_seq,
        )

    def create_saved_view(
        config: SavedViewInput,
        visualization_session_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ):
        """Persist a SavedView from explicit node IDs and optional positions; no browser needed."""
        return create(
            SavedViewInput,
            config,
            visualization_session_id,
            expected_session_seq,
        )

    def create_collection_response(
        config: CollectionResponseInput,
        visualization_session_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ):
        """Create a CollectionResponse with validated answers and a link to its collection."""
        return create(
            CollectionResponseInput,
            config,
            visualization_session_id,
            expected_session_seq,
        )

    return {
        tool.__name__: tool
        for tool in (
            create_agent,
            create_event_subscription,
            create_skill,
            create_active_knowledge_collection,
            create_saved_view,
            create_collection_response,
        )
    }


def system_create_tool_definitions():
    """The assistant uses the same input models FastMCP advertises."""
    tools = make_system_create_tools(None)
    definitions = []
    for name, model in SYSTEM_CREATE_MODELS.items():
        schema = model.model_json_schema()
        definitions.append(
            {
                "name": name,
                "description": tools[name].__doc__,
                "input_schema": {
                    "type": "object",
                    "$defs": schema.pop("$defs", {}),
                    "properties": {"config": schema},
                    "required": ["config"],
                },
            }
        )
    return definitions
