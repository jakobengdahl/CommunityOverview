"""
Tests for the generic, config-neutral tag/metadata filters on search_graph.

These cover the mechanism added to let an agent filter search results by
arbitrary tags (include/exclude, AND/OR) and arbitrary metadata key/value(s)
without the engine hardcoding any planning-specific field names or values.
"""

from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService


def _service(nodes, edges=None) -> GraphService:
    # empty_storage-style construction via the shared fixture would work too, but
    # these tests build their own small graphs for locality.
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    storage = GraphStorage(
        json_path=os.path.join(tmp, "g.json"),
        embeddings_path=os.path.join(tmp, "e.pkl"),
    )
    storage.add_nodes(nodes, edges or [])
    return GraphService(storage)


def _names(result):
    return {n["name"] for n in result["nodes"]}


def _tagged_service() -> GraphService:
    return _service(
        [
            Node(id="a1", type=NodeType.ACTOR, name="Alpha", tags=["red"]),
            Node(id="a2", type=NodeType.ACTOR, name="Beta", tags=["blue"]),
            Node(
                id="a3",
                type=NodeType.ACTOR,
                name="Gamma",
                tags=["red", "blue"],
            ),
            Node(id="a4", type=NodeType.ACTOR, name="Delta", tags=[]),
        ]
    )


class TestNoFilterUnchanged:
    def test_empty_query_returns_all(self):
        service = _tagged_service()
        result = service.search_graph(query="")
        assert _names(result) == {"Alpha", "Beta", "Gamma", "Delta"}

    def test_filters_echo_empty_by_default(self):
        service = _tagged_service()
        result = service.search_graph(query="")
        assert result["filters"]["tags_any"] == []
        assert result["filters"]["tags_all"] == []
        assert result["filters"]["tags_none"] == []
        assert result["filters"]["metadata_filters"] == []


class TestTagFilters:
    def test_tags_any_is_or(self):
        service = _tagged_service()
        result = service.search_graph(query="", tags_any=["red", "blue"])
        assert _names(result) == {"Alpha", "Beta", "Gamma"}

    def test_tags_all_is_and(self):
        service = _tagged_service()
        result = service.search_graph(query="", tags_all=["red", "blue"])
        assert _names(result) == {"Gamma"}

    def test_tags_none_excludes(self):
        service = _tagged_service()
        result = service.search_graph(query="", tags_none=["red"])
        assert _names(result) == {"Beta", "Delta"}

    def test_tags_any_and_none_combine(self):
        service = _tagged_service()
        # OR over {red,blue} then drop anything carrying red → only Beta.
        result = service.search_graph(
            query="", tags_any=["red", "blue"], tags_none=["red"]
        )
        assert _names(result) == {"Beta"}

    def test_tag_filter_composes_with_text_query(self):
        service = _service(
            [
                Node(id="n1", type=NodeType.ACTOR, name="Solar plant", tags=["energy"]),
                Node(
                    id="n2", type=NodeType.ACTOR, name="Solar panel", tags=["hardware"]
                ),
            ]
        )
        result = service.search_graph(query="Solar", tags_any=["energy"])
        assert _names(result) == {"Solar plant"}


class TestMetadataFilters:
    def _meta_service(self) -> GraphService:
        return _service(
            [
                Node(
                    id="m1",
                    type=NodeType.ACTOR,
                    name="One",
                    metadata={"status": "open", "priority": 1},
                ),
                Node(
                    id="m2",
                    type=NodeType.ACTOR,
                    name="Two",
                    metadata={"status": "done", "priority": 2},
                ),
                Node(
                    id="m3",
                    type=NodeType.ACTOR,
                    name="Three",
                    metadata={"labels": ["x", "y"]},
                ),
                Node(id="m4", type=NodeType.ACTOR, name="Four", metadata={}),
            ]
        )

    def test_metadata_any_scalar_match(self):
        service = self._meta_service()
        result = service.search_graph(
            query="",
            metadata_filters=[{"key": "status", "values": ["open"]}],
        )
        assert _names(result) == {"One"}

    def test_metadata_any_multiple_values(self):
        service = self._meta_service()
        result = service.search_graph(
            query="",
            metadata_filters=[{"key": "status", "values": ["open", "done"]}],
        )
        assert _names(result) == {"One", "Two"}

    def test_metadata_none_excludes(self):
        service = self._meta_service()
        result = service.search_graph(
            query="",
            metadata_filters=[{"key": "status", "values": ["done"], "match": "none"}],
        )
        # Two carries status=done → excluded; nodes without the key survive.
        assert _names(result) == {"One", "Three", "Four"}

    def test_metadata_all_on_list_value(self):
        service = self._meta_service()
        result = service.search_graph(
            query="",
            metadata_filters=[{"key": "labels", "values": ["x", "y"], "match": "all"}],
        )
        assert _names(result) == {"Three"}

    def test_metadata_value_compared_as_string(self):
        service = self._meta_service()
        # Stored priority is the int 2; a filter value of "2" must still match.
        result = service.search_graph(
            query="",
            metadata_filters=[{"key": "priority", "values": ["2"]}],
        )
        assert _names(result) == {"Two"}

    def test_metadata_and_tag_filters_combine(self):
        service = _service(
            [
                Node(
                    id="c1",
                    type=NodeType.ACTOR,
                    name="Keeper",
                    tags=["team-a"],
                    metadata={"status": "open"},
                ),
                Node(
                    id="c2",
                    type=NodeType.ACTOR,
                    name="Dropped-tag",
                    tags=["team-b"],
                    metadata={"status": "open"},
                ),
                Node(
                    id="c3",
                    type=NodeType.ACTOR,
                    name="Dropped-meta",
                    tags=["team-a"],
                    metadata={"status": "done"},
                ),
            ]
        )
        result = service.search_graph(
            query="",
            tags_any=["team-a"],
            metadata_filters=[{"key": "status", "values": ["open"]}],
        )
        assert _names(result) == {"Keeper"}

    def test_empty_values_filter_is_ignored(self):
        service = self._meta_service()
        # A filter with no values imposes no constraint (unchanged behaviour).
        result = service.search_graph(
            query="",
            metadata_filters=[{"key": "status", "values": []}],
        )
        assert _names(result) == {"One", "Two", "Three", "Four"}
