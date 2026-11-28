"""Serve Skill nodes from the graph as skills.

The MCP catalog, the agent worker and the built-in assistant's expert agents
all read Skill nodes through one ``GraphSkillProvider``, so every surface
applies the same sanitisation to the same node content.
"""

import logging
import re
from types import SimpleNamespace
from typing import Optional, Sequence

import yaml

from backend.skills.loader import SkillDefinition, SkillsLoader
from backend.skills.mcp import MAX_BYTES, MAX_FILES, SkillFiles, SkillsExtension

logger = logging.getLogger(__name__)

SKILL_PATH = "skill_path"

_RESERVED_ROOTS = ("graph-working-method", "graph-semantics")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)", re.M | re.S)
_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def _is_skill(node) -> bool:
    return getattr(node.type, "value", node.type) == "Skill"


def _allowed_tools(meta) -> list:
    tools = meta.get("allowed_tools")
    return [str(t) for t in tools] if isinstance(tools, list) else []


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")


class _One:
    def __init__(self, skill):
        self.skill = skill

    def skills(self):
        return [self.skill]


class GraphSkillProvider:
    """Supply readable Skill nodes; one snapshot per ``skills()`` call."""

    def __init__(
        self,
        storage,
        authorization_hook=None,
        attachment_settings=None,
        ttl_ms: int = 0,
        cache_scope: str = "private",
    ):
        self.storage = storage
        self.authorization_hook = authorization_hook
        # None when attachment content may not be served to MCP callers.
        self.attachment_settings = attachment_settings
        self.ttl_ms = ttl_ms
        # Access varies per node, so a shared cache would leak skills.
        self.cache_scope = cache_scope
        self.node_ids: dict = {}
        self.with_attachments: set = set()
        # Served attachment file URI -> (node id, attachment record), for audit.
        self.attachment_records: dict = {}
        # Node id -> root it was last served under; incumbents win clashes.
        self._roots: dict = {}
        self._sanitizer = SkillsLoader()

    def _body(self, node) -> str:
        content = (node.metadata or {}).get("content")
        if not isinstance(content, str):
            return ""
        return self._sanitizer._sanitize(_FRONTMATTER.sub("", content, count=1))

    def _optional_text(self, value) -> Optional[str]:
        if not isinstance(value, str):
            return None
        return self._sanitizer._sanitize(value) or None

    def skill_definition(
        self, node_id: str, max_chars: Optional[int] = None
    ) -> Optional[SkillDefinition]:
        """The node as a prompt skill, or None when it is no usable Skill node."""
        node = self.storage.get_node(node_id)
        if node is None or not _is_skill(node):
            return None
        body = self._body(node)
        if not body:
            return None
        meta = node.metadata or {}
        name, description = node.name or "", node.description or ""
        tools = _allowed_tools(meta)
        # Like the served frontmatter: a name, description or allowed-tools the
        # sanitizer would change rejects the skill rather than being rewritten.
        if any(
            self._sanitizer._sanitize(v) != v.strip()
            for v in (name, description, " ".join(tools))
        ):
            return None
        return SkillDefinition(
            id=node.id,
            name=name.strip(),
            description=description.strip(),
            content=body[:max_chars] if max_chars else body,
            allowed_tools=tools,
            when_to_use=self._optional_text(meta.get("when_to_use")),
            effort=self._optional_text(meta.get("effort")),
            source_url=meta.get("source_url") or f"graph://skill/{node.id}",
        )

    def _attachments(self, node, records_out: dict) -> dict:
        settings = self.attachment_settings
        if settings is None:
            return {}
        records = sorted(
            settings.store.list(settings.namespace, node.id), key=lambda r: r.id
        )
        # SKILL.md takes one of the file slots.
        if len(records) >= MAX_FILES or sum(r.size for r in records) > MAX_BYTES:
            raise ValueError("Skill exceeds size limit")
        files = {}
        for record in records:
            path = "references/" + (
                _PATH_CHARS.sub("-", record.filename).strip(".-") or record.id
            )
            with settings.store.open_read(
                settings.namespace, node.id, record.id
            ) as stream:
                data = stream.read(MAX_BYTES + 1)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue  # Skill files are text; binary attachments are not served.
            if path not in files:
                files[path] = text
                records_out[path] = record
        return files

    def _files(self, node, records_out: dict) -> SkillFiles:
        meta = node.metadata or {}
        path = meta.get(SKILL_PATH)
        path = path.strip("/") if isinstance(path, str) and path.strip("/") else ""
        name = path.rsplit("/", 1)[-1] if path else _slug(node.name or "")
        description = node.description or meta.get("when_to_use") or ""
        frontmatter = {"name": name, "description": description[:1024].strip()}
        tools = _allowed_tools(meta)
        if tools:
            frontmatter["allowed-tools"] = " ".join(tools)
        metadata = {"node-id": node.id, "revision": str(node.revision)}
        if meta.get("version"):
            metadata["version"] = str(meta["version"])
        frontmatter["metadata"] = metadata
        body = self._body(node)
        if not body:
            raise ValueError("Skill content rejected")
        text = (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
            + "---\n"
            + body
        )
        files = self._attachments(node, records_out)
        return SkillFiles(name, {**files, "SKILL.md": text}, path)

    def skills(self) -> Sequence[SkillFiles]:
        candidates = []
        for node in sorted(self.storage.get_all_nodes(), key=lambda n: n.id):
            if not _is_skill(node) or node.archived:
                continue
            if not (node.metadata or {}).get("content"):
                continue
            records = {}
            try:
                skill = self._files(node, records)
                # The catalog's own validation, one skill at a time, so one bad
                # node cannot take every other skill down with it.
                SkillsExtension([_One(skill)], 0, "private")
            except Exception as exc:
                logger.warning("Skill node %s not served: %s", node.id, exc)
                continue
            candidates.append((node, skill, skill.path or skill.name, records))

        def clash(a, b) -> bool:
            return (
                a[1].name == b[1].name
                or a[2] == b[2]
                or a[2].startswith(b[2] + "/")
                or b[2].startswith(a[2] + "/")
            )

        # On a name or path clash the skill already served under that root
        # keeps it; node fields such as created_at are caller-supplied, so no
        # newcomer can claim precedence. Clashing newcomers are all withheld.
        incumbents = [c for c in candidates if self._roots.get(c[0].id) == c[2]]
        newcomers = [c for c in candidates if self._roots.get(c[0].id) != c[2]]
        reserved = [(None, SimpleNamespace(name=r), r) for r in _RESERVED_ROOTS]
        served = []
        for candidate in incumbents + newcomers:
            rivals = reserved + served
            if candidate in newcomers:
                rivals += [c for c in newcomers if c is not candidate]
            if any(clash(candidate, other) for other in rivals):
                logger.warning("Skill node %s not served: path in use", candidate[0].id)
                continue
            served.append(candidate)

        node_ids, with_attachments, attachment_records = {}, set(), {}
        for node, skill, root, records in served:
            node_ids[skill.name] = node.id
            if len(skill.files) > 1:
                with_attachments.add(skill.name)
            for path, record in records.items():
                attachment_records[f"skill://{root}/{path}"] = (node.id, record)
        self.node_ids, self.with_attachments = node_ids, with_attachments
        self.attachment_records = attachment_records
        self._roots = {node.id: root for node, _, root, _ in served}
        return [skill for _, skill, _, _ in served]

    def file_available(self, uri: str) -> bool:
        """Whether a served attachment file still exists and opens now."""
        found = self.attachment_records.get(uri)
        if found is None:
            return True
        from backend.core.attachment_store import AttachmentNotFound

        settings = self.attachment_settings
        try:
            settings.store.open_read(settings.namespace, found[0], found[1].id).close()
        except (AttachmentNotFound, OSError):
            return False
        return True

    def audit_read(self, uri: str, text: str) -> None:
        """Record a served attachment file like an attachment content read."""
        found = self.attachment_records.get(uri)
        if found is not None:
            from backend.service.attachment_tools import AttachmentReadTools

            AttachmentReadTools.audit("read", *found, len(text))

    def audit_refusal(self, uri: str) -> None:
        """Record a denied attachment file read like a refused attachment read."""
        if uri in self.attachment_records:
            from backend.service.attachment_tools import audit_attachment_refusal

            audit_attachment_refusal("read_node_attachment", "access_denied")

    async def can_read(self, ctx, skill_name: str) -> bool:
        from backend.runtime.authorization import GRAPH_ACTION_READ
        from backend.service.access import evaluate_graph_access, is_node_visible

        node = self.storage.get_node(self.node_ids.get(skill_name, ""))
        if node is None or not _is_skill(node) or node.archived:
            return False
        if self.authorization_hook is None:
            return True
        try:
            # Node files need what get_node_details needs; attachment files also
            # the grants and narrowing read_node_attachment applies
            # (AttachmentReadTools.authorize and resolve).
            targets = ["get_node_details"]
            if skill_name in self.with_attachments:
                targets += ["get_node_attachment", "read_node_attachment_content"]
            for target in targets:
                decision = evaluate_graph_access(
                    self.authorization_hook, action=GRAPH_ACTION_READ, target=target
                )
                if not decision.allowed or not is_node_visible(
                    node, decision.graph_access
                ):
                    return False
            return True
        except Exception:
            return False
