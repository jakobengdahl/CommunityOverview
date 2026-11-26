"""Versioned, self-contained template documents and their persistence seam."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from backend.config.config_loader import SYSTEM_NODE_TYPES
from backend.config.node_shapes import ShapeDefinition, ShapeSelection
from .session_annotations import (
    _validate_generic_content,
    annotation_link_fields,
    annotation_type_of,
    image_annotation_error,
    resolve_annotation_type_alias,
)
from .session_store import (
    _DEFAULT_MAX_ANNOTATIONS,
    _validate_annotation,
    is_valid_session_id,
    SessionPersistenceBackend,
)

MAX_TEMPLATE_NODES = 500
MAX_TEMPLATE_EDGES = 2000
# Deliberately small: graph metadata can contain operational or private state.
ALLOWED_METADATA = frozenset({"color", "icon"})


class TemplateError(ValueError):
    """Invalid document or an unavailable revision."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Position(StrictModel):
    x: float
    y: float


class Bounds(StrictModel):
    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class Snapshot(StrictModel):
    type: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    summary: str
    tags: list[str]
    subtypes: list[str]
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def allowed_content(self):
        if self.type in SYSTEM_NODE_TYPES or not set(self.metadata) <= ALLOWED_METADATA:
            raise ValueError("disallowed node content")
        return self


class TemplateShape(StrictModel):
    ref: str | None
    inline: ShapeDefinition | None
    builtin: bool

    @model_validator(mode="after")
    def valid_selection(self):
        if self.builtin:
            if self.ref is not None or self.inline is not None:
                raise ValueError("builtin cannot have a fallback")
        elif self.inline is None:
            raise ValueError("inline fallback required")
        if self.ref is not None:
            ShapeSelection.model_validate({"ref": self.ref})
        return self


class TemplateNode(StrictModel):
    key: str = Field(min_length=1)
    source_node_id: str = Field(min_length=1)
    snapshot: Snapshot
    position: Position
    hidden: bool
    shape: TemplateShape | None


class TemplateEdge(StrictModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: str = Field(min_length=1)
    # A source edge ID and label preserve information without importing graph state.
    source_edge_id: str = Field(min_length=1)
    label: str


class VersionedDocument(StrictModel):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid version")
        return value


class AnnotationDocument(VersionedDocument):
    annotations: list[dict[str, Any]]


class TemplateDocument(VersionedDocument):
    template_id: str
    name: str = Field(min_length=1)
    description: str
    tags: list[str]
    revision: int = Field(ge=0)
    created_by: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    source_session_revision: int = Field(ge=0)
    nodes: list[TemplateNode]
    edges: list[TemplateEdge]
    annotation_document: AnnotationDocument
    bounds: Bounds
    thumbnail: None

    @model_validator(mode="after")
    def references(self):
        if not is_valid_session_id(self.template_id):
            raise ValueError("invalid template id")
        keys = {node.key for node in self.nodes}
        if len(keys) != len(self.nodes):
            raise ValueError("duplicate node key")
        if any(
            edge.source not in keys or edge.target not in keys for edge in self.edges
        ):
            raise ValueError("edge outside template")
        annotations = self.annotation_document.annotations
        ids = [a.get("id") for a in annotations]
        if any(not isinstance(i, str) or not i or i in keys for i in ids) or len(
            set(ids)
        ) != len(ids):
            raise ValueError("invalid annotation id")
        node_or_annotation = keys | set(ids)
        for annotation in annotations:
            _validate_annotation(annotation, require_id=True)
            content = annotation.get("content")
            if content is not None and not isinstance(content, dict):
                raise ValueError("invalid annotation content")
            # A key set in both places would let one copy hide the other from
            # the checks below while a reader of the other copy uses it.
            if content and content.keys() & annotation.keys():
                raise ValueError("ambiguous annotation content")
            # type and kind are aliases: the canvas resolves the type from the
            # top level only, so content must not claim a different one.
            ann_type = annotation_type_of(annotation)
            if content and any(
                key in content
                and resolve_annotation_type_alias(content[key]) != ann_type
                for key in ("type", "kind")
            ):
                raise ValueError("ambiguous annotation content")
            payload = {**annotation, **(content or {}), "type": ann_type}
            if _validate_generic_content(ann_type, payload):
                raise ValueError("invalid annotation content")
            if image_annotation_error(payload):
                raise ValueError("invalid image annotation")
            members = payload.get("member_node_ids", [])
            if not isinstance(members, list) or any(
                not isinstance(k, str) or not k for k in members
            ):
                raise ValueError("invalid group members")
            for holder, key, declared in annotation_link_fields(payload):
                value = holder[key] if isinstance(holder, list) else holder.get(key)
                if value is None or value == "":
                    continue
                if isinstance(holder, list) or declared == "node":
                    valid = keys
                elif declared == "annotation":
                    valid = set(ids)
                else:
                    valid = node_or_annotation if declared is None else set()
                if not isinstance(value, str) or value not in valid:
                    raise ValueError("invalid annotation reference")
        return self


def _has_non_string_key(value: Any) -> bool:
    # json.dumps coerces int/bool/None keys to strings, so a stored document
    # would read back different from (or with fewer entries than) the input.
    if isinstance(value, dict):
        return any(
            not isinstance(k, str) or _has_non_string_key(v) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_has_non_string_key(v) for v in value)
    return False


def validate_template(document: Any) -> dict[str, Any]:
    """Validate without normalizing, filtering, or changing the supplied JSON."""
    try:
        # Check limits before model parsing so the limit error is stable.
        if isinstance(document, dict):
            for value, limit, code in (
                (document.get("nodes"), MAX_TEMPLATE_NODES, "nodes_limit"),
                (document.get("edges"), MAX_TEMPLATE_EDGES, "edges_limit"),
                (
                    (document.get("annotation_document") or {}).get("annotations")
                    if isinstance(document.get("annotation_document"), dict)
                    else None,
                    _DEFAULT_MAX_ANNOTATIONS,
                    "annotations_limit",
                ),
            ):
                if isinstance(value, list) and len(value) > limit:
                    raise TemplateError(code)
        json.dumps(document, allow_nan=False)
        if _has_non_string_key(document):
            raise TemplateError("invalid_document")
        TemplateDocument.model_validate(document)
        return copy.deepcopy(document)
    except TemplateError:
        raise
    # RecursionError: free-form payloads nested past the interpreter limit.
    except (
        ValidationError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise TemplateError("invalid_document") from exc


class TemplateStore:
    """Use a dedicated session-compatible backend; its transaction serializes CAS."""

    def __init__(self, backend: SessionPersistenceBackend):
        self._backend = backend

    def get(self, template_id: str) -> dict[str, Any] | None:
        if not is_valid_session_id(template_id):
            raise TemplateError("invalid_id")
        with self._backend.transaction():
            record = self._backend.load(template_id)
            if record is None or record.get("deleted"):
                return None
            return validate_template(record["document"])

    def list(self) -> list[dict[str, Any]]:
        with self._backend.transaction():
            return [
                doc
                for key in sorted(self._backend.list_ids())
                if (doc := self.get(key)) is not None
            ]

    def save(self, document: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        doc = validate_template(document)
        if type(expected_revision) is not int or expected_revision < 0:
            raise TemplateError("invalid_revision")
        with self._backend.transaction():
            old = self._backend.load(doc["template_id"])
            if old and old.get("deleted"):
                raise TemplateError("deleted")
            revision = old["document"]["revision"] if old else 0
            if expected_revision != revision or doc["revision"] != expected_revision:
                raise TemplateError("revision_conflict")
            doc["revision"] = revision + 1
            self._backend.save({"id": doc["template_id"], "document": doc})
        return copy.deepcopy(doc)

    def delete(self, template_id: str, expected_revision: int) -> None:
        if not is_valid_session_id(template_id):
            raise TemplateError("invalid_id")
        with self._backend.transaction():
            old = self._backend.load(template_id)
            if not old or old.get("deleted"):
                raise TemplateError("not_found")
            if (
                type(expected_revision) is not int
                or old["document"]["revision"] != expected_revision
            ):
                raise TemplateError("revision_conflict")
            self._backend.save(
                {"id": template_id, "deleted": True, "revision": expected_revision + 1}
            )
