"""
Unit tests for GraphService.

Tests the business logic layer in isolation.
"""

from backend.service import GraphService


class TestGraphServiceSearch:
    """Tests for search operations."""

    def test_search_graph_by_query(self, populated_service: GraphService):
        """Test searching nodes by text query."""
        result = populated_service.search_graph(query="Skatteverket")

        assert "nodes" in result
        assert "edges" in result
        assert result["total"] >= 1
        assert any(n["name"] == "Skatteverket" for n in result["nodes"])

    def test_search_graph_with_type_filter(self, populated_service: GraphService):
        """Test filtering search by node type."""
        result = populated_service.search_graph(query="", node_types=["Actor"])

        assert result["total"] >= 1
        assert all(n["type"] == "Actor" for n in result["nodes"])

    def test_search_graph_returns_all_with_empty_query(
        self, populated_service: GraphService
    ):
        """Test that empty query returns all nodes."""
        result = populated_service.search_graph(query="")

        assert result["total"] >= 1

    def test_search_graph_returns_edges(self, populated_service: GraphService):
        """Test that search includes edges between result nodes."""
        result = populated_service.search_graph(query="")

        # Empty query returns all nodes; edges connecting them should be included
        assert len(result["nodes"]) >= 2
        assert "edges" in result

    def test_search_graph_with_limit(self, populated_service: GraphService):
        """Test search result limit."""
        result = populated_service.search_graph(query="", limit=2)

        assert result["total"] <= 2

    def test_get_node_details_success(self, populated_service: GraphService):
        """Test getting node details for existing node."""
        result = populated_service.get_node_details("actor-1")

        assert result["success"] is True
        assert result["node"]["id"] == "actor-1"
        assert result["node"]["name"] == "Skatteverket"

    def test_get_node_details_not_found(self, populated_service: GraphService):
        """Test getting node details for non-existent node."""
        result = populated_service.get_node_details("nonexistent")

        assert result["success"] is False
        assert "error" in result

    def test_get_related_nodes(self, populated_service: GraphService):
        """Test getting related nodes."""
        result = populated_service.get_related_nodes("actor-1", depth=1)

        assert "nodes" in result
        assert "edges" in result
        # actor-1 is connected to init-1
        node_ids = [n["id"] for n in result["nodes"]]
        assert "init-1" in node_ids

    def test_get_related_nodes_with_depth(self, populated_service: GraphService):
        """Test getting related nodes with depth > 1."""
        result = populated_service.get_related_nodes("actor-1", depth=2)

        # With depth 2, should reach community-1 through init-1
        node_ids = [n["id"] for n in result["nodes"]]
        assert "community-1" in node_ids

    def test_get_related_nodes_with_relationship_filter(
        self, populated_service: GraphService
    ):
        """Test filtering related nodes by relationship type."""
        result = populated_service.get_related_nodes(
            "init-1", relationship_types=["GOVERNED_BY"], depth=1
        )

        # Should only find legislation-1 via GOVERNED_BY
        node_ids = [n["id"] for n in result["nodes"]]
        assert "legislation-1" in node_ids
        # Should not find actors (BELONGS_TO)
        edge_types = [e["type"] for e in result["edges"]]
        assert all(t == "GOVERNED_BY" for t in edge_types)


class TestGraphServiceSimilarity:
    """Tests for similarity operations."""

    def test_find_similar_nodes(self, populated_service: GraphService):
        """Test finding similar nodes by name."""
        result = populated_service.find_similar_nodes(
            name="Skatteverk",  # Similar to "Skatteverket"
            threshold=0.5,
        )

        assert "similar_nodes" in result
        assert result["total"] >= 1

    def test_find_similar_nodes_with_type_filter(self, populated_service: GraphService):
        """Test filtering similar nodes by type."""
        result = populated_service.find_similar_nodes(
            name="Digital", node_type="Initiative", threshold=0.3
        )

        # All results should be initiatives
        for item in result["similar_nodes"]:
            assert item["node"]["type"] == "Initiative"

    def test_find_similar_nodes_batch(self, populated_service: GraphService):
        """Test batch similarity search."""
        result = populated_service.find_similar_nodes_batch(
            names=["Skatteverk", "Bolagsverket", "Unknown"], threshold=0.5
        )

        assert "results" in result
        assert "Skatteverk" in result["results"]
        assert "Bolagsverket" in result["results"]
        assert "Unknown" in result["results"]
        assert result["total_searched"] == 3


class TestGraphServiceCRUD:
    """Tests for CRUD operations."""

    def test_add_nodes(self, empty_service: GraphService):
        """Test adding nodes."""
        nodes = [{"type": "Actor", "name": "New Actor", "description": "A new actor"}]
        result = empty_service.add_nodes(nodes=nodes, edges=[])

        assert result["success"] is True
        assert len(result["added_node_ids"]) == 1

    def test_add_nodes_with_edges(self, empty_service: GraphService):
        """Test adding nodes with edges."""
        nodes = [
            {"id": "n1", "type": "Actor", "name": "Actor 1"},
            {"id": "n2", "type": "Initiative", "name": "Init 1"},
        ]
        edges = [{"source": "n1", "target": "n2", "type": "BELONGS_TO"}]
        result = empty_service.add_nodes(nodes=nodes, edges=edges)

        assert result["success"] is True
        assert len(result["added_node_ids"]) == 2
        assert len(result["added_edge_ids"]) == 1

    def test_agent_batch_write_rejects_inapplicable_edge(
        self, empty_service: GraphService
    ):
        """Agent/MCP-style add_nodes writes return a clear applicability error."""
        result = empty_service.add_nodes(
            nodes=[
                {"id": "actor-1", "type": "Actor", "name": "Actor 1"},
                {"id": "law-1", "type": "Legislation", "name": "Law 1"},
            ],
            edges=[
                {
                    "source": "actor-1",
                    "target": "law-1",
                    "type": "IMPLEMENTS",
                }
            ],
            event_origin="agent:test-agent",
        )

        assert result["success"] is False
        assert "source type 'Actor' is not allowed" in result["message"]

    def test_add_edge_rejects_inapplicable_relationship_type(
        self, empty_service: GraphService
    ):
        """Single edge API writes surface applicability failures cleanly."""
        empty_service.add_nodes(
            nodes=[
                {"id": "actor-1", "type": "Actor", "name": "Actor 1"},
                {"id": "law-1", "type": "Legislation", "name": "Law 1"},
            ],
            edges=[],
        )

        result = empty_service.add_edge("actor-1", "law-1", type="IMPLEMENTS")

        assert result["success"] is False
        assert "source type 'Actor' is not allowed" in result["message"]

    def test_add_nodes_dynamic_type_accepted(self, empty_service: GraphService):
        """Dynamic node types are accepted (schema is permissive)."""
        nodes = [{"type": "CustomType", "name": "Test"}]
        result = empty_service.add_nodes(nodes=nodes, edges=[])

        assert result["success"] is True
        assert len(result["added_node_ids"]) == 1

    def test_update_node(self, populated_service: GraphService):
        """Test updating a node."""
        result = populated_service.update_node(
            "actor-1",
            {"description": "Updated description", "tags": ["updated", "tax"]},
        )

        assert result["success"] is True
        assert result["node"]["description"] == "Updated description"
        assert "updated" in result["node"]["tags"]

    def test_update_node_not_found(self, populated_service: GraphService):
        """Test updating non-existent node."""
        result = populated_service.update_node("nonexistent", {"name": "New"})

        assert result["success"] is False
        assert "error" in result

    def test_update_node_metadata_merge_through_service(
        self, populated_service: GraphService
    ):
        """Merge mode is honoured end-to-end through the service layer."""
        populated_service.update_node("actor-1", {"metadata": {"a": 1, "b": 2}})
        result = populated_service.update_node(
            "actor-1", {"metadata": {"a": 9, "b": None}}, metadata_merge=True
        )

        assert result["success"] is True
        assert result["node"]["metadata"] == {"a": 9}

    def test_update_node_optimistic_conflict_returns_409_shape(
        self, populated_service: GraphService
    ):
        """A stale expected_updated_at surfaces as a conflict result, not a clobber."""
        first = populated_service.update_node("actor-1", {"summary": "one"})
        stale = first["node"]["updated_at"]
        populated_service.update_node("actor-1", {"summary": "two"})

        result = populated_service.update_node(
            "actor-1", {"summary": "three"}, expected_updated_at=stale
        )

        assert result["success"] is False
        assert result["conflict"] is True
        assert "current_updated_at" in result
        # The rejected write must not have applied.
        fresh = populated_service.update_node(
            "actor-1",
            {"summary": "three"},
            expected_updated_at=result["current_updated_at"],
        )
        assert fresh["success"] is True

    def test_update_node_over_limit_returns_clean_error(
        self, populated_service: GraphService
    ):
        """An over-limit field update is rejected with a clean error, not a 500 or a
        silently-persisted value that would fail the next graph load."""
        result = populated_service.update_node("actor-1", {"description": "x" * 2001})

        assert result["success"] is False
        assert "error" in result
        assert "validating input" in result["error"].lower()

    def test_delete_nodes(self, populated_service: GraphService):
        """Test deleting nodes."""
        result = populated_service.delete_nodes(["actor-1"], confirmed=True)

        assert result["success"] is True
        assert "actor-1" in result["deleted_node_ids"]
        # Related edges should be affected
        assert len(result["affected_edge_ids"]) > 0

    def test_delete_nodes_requires_confirmation(self, populated_service: GraphService):
        """Test that deletion requires confirmation."""
        result = populated_service.delete_nodes(["actor-1"], confirmed=False)

        assert result["success"] is False
        # Node should still exist
        node_result = populated_service.get_node_details("actor-1")
        assert node_result["success"] is True

    def test_delete_nodes_max_limit(self, populated_service: GraphService):
        """Test that deletion is limited to 10 nodes."""
        node_ids = [f"node-{i}" for i in range(15)]
        result = populated_service.delete_nodes(node_ids, confirmed=True)

        assert result["success"] is False
        assert "Max 10" in result["message"]

    def test_delete_edge(self, empty_service: GraphService):
        """Test deleting a single edge."""
        empty_service.add_nodes(
            nodes=[
                {"id": "actor-1", "type": "Actor", "name": "Actor 1"},
                {"id": "init-1", "type": "Initiative", "name": "Initiative 1"},
            ],
            edges=[
                {
                    "id": "edge-1",
                    "source": "actor-1",
                    "target": "init-1",
                    "type": "RELATES_TO",
                }
            ],
        )

        result = empty_service.delete_edge("edge-1")

        assert result["success"] is True
        assert result["deleted_edge_id"] == "edge-1"

    def test_update_edge_rejects_inapplicable_type_change(
        self, empty_service: GraphService
    ):
        """Changing an edge type is validated against endpoint node types."""
        empty_service.add_nodes(
            nodes=[
                {"id": "actor-1", "type": "Actor", "name": "Actor 1"},
                {"id": "law-1", "type": "Legislation", "name": "Law 1"},
            ],
            edges=[
                {
                    "id": "edge-1",
                    "source": "actor-1",
                    "target": "law-1",
                    "type": "RELATES_TO",
                }
            ],
        )

        result = empty_service.update_edge("edge-1", {"type": "IMPLEMENTS"})

        assert result["success"] is False
        assert "source type 'Actor' is not allowed" in result["message"]

    def test_delete_edges_bulk(self, empty_service: GraphService):
        """Test deleting multiple edges."""
        empty_service.add_nodes(
            nodes=[
                {"id": "actor-1", "type": "Actor", "name": "Actor 1"},
                {"id": "init-1", "type": "Initiative", "name": "Initiative 1"},
                {"id": "res-1", "type": "Resource", "name": "Resource 1"},
            ],
            edges=[
                {
                    "id": "edge-1",
                    "source": "actor-1",
                    "target": "init-1",
                    "type": "RELATES_TO",
                },
                {
                    "id": "edge-2",
                    "source": "init-1",
                    "target": "res-1",
                    "type": "RELATES_TO",
                },
            ],
        )

        result = empty_service.delete_edges(["edge-1", "edge-2"], confirmed=True)

        assert result["success"] is True
        assert set(result["deleted_edge_ids"]) == {"edge-1", "edge-2"}

    def test_delete_edges_max_limit(self, empty_service: GraphService):
        """Test that edge deletion is limited to 50 edges."""
        result = empty_service.delete_edges(
            [f"edge-{i}" for i in range(60)], confirmed=True
        )

        assert result["success"] is False
        assert "Max 50" in result["message"]

    def test_delete_edges_limit_checked_before_confirmation(
        self, empty_service: GraphService
    ):
        """Max-limit error must take precedence over the confirmation prompt."""
        # confirmed=False — before the fix, this returned the confirmation error.
        result = empty_service.delete_edges(
            [f"edge-{i}" for i in range(60)], confirmed=False
        )

        assert result["success"] is False
        assert "Max 50" in result["message"], (
            f"Expected limit error but got: {result['message']!r}"
        )


class TestGraphServiceStatistics:
    """Tests for statistics and metadata operations."""

    def test_get_graph_stats(self, populated_service: GraphService):
        """Test getting graph statistics."""
        result = populated_service.get_graph_stats()

        assert "total_nodes" in result
        assert "total_edges" in result
        assert "nodes_by_type" in result
        assert result["total_nodes"] == 5
        assert result["total_edges"] == 4

    def test_get_graph_stats_has_type_counts(self, populated_service: GraphService):
        """Test that stats include node counts by type."""
        result = populated_service.get_graph_stats()

        assert "Actor" in result["nodes_by_type"]
        assert result["nodes_by_type"]["Actor"] == 2

    def test_get_graph_stats_has_edge_type_counts(self, populated_service: GraphService):
        """Test that stats include edge counts by relationship type."""
        result = populated_service.get_graph_stats()

        assert "edges_by_type" in result
        assert result["edges_by_type"]["BELONGS_TO"] == 2
        assert result["edges_by_type"]["PART_OF"] == 1

    def test_list_node_types(self, empty_service: GraphService):
        """Test listing node types."""
        result = empty_service.list_node_types()

        assert "node_types" in result
        types = [t["type"] for t in result["node_types"]]
        assert "Actor" in types
        assert "Initiative" in types
        assert "Capability" in types
        # Each type should have color and description
        for nt in result["node_types"]:
            assert "color" in nt
            assert "description" in nt

    def test_list_relationship_types(self, empty_service: GraphService):
        """Test listing relationship types."""
        result = empty_service.list_relationship_types()

        assert "relationship_types" in result
        types = [t["type"] for t in result["relationship_types"]]
        assert "BELONGS_TO" in types
        assert "IMPLEMENTS" in types
        # Each type should have description
        for rt in result["relationship_types"]:
            assert "description" in rt


class TestGraphServiceSavedViews:
    """Tests for saved view operations."""

    def test_save_view_returns_signal(self, empty_service: GraphService):
        """Test that save_view returns a signal for the frontend."""
        result = empty_service.save_view("My View")

        assert result["action"] == "save_view"
        assert result["name"] == "My View"
        assert "message" in result

    def test_get_saved_view(self, service_with_view: GraphService):
        """Test loading a saved view."""
        result = service_with_view.get_saved_view("Test View")

        assert result["success"] is True
        assert "nodes" in result
        assert "edges" in result
        assert "positions" in result
        assert result["action"] == "load_visualization"

    def test_get_saved_view_not_found(self, populated_service: GraphService):
        """Test loading a non-existent view."""
        result = populated_service.get_saved_view("Nonexistent View")

        assert result["success"] is False
        assert "error" in result

    def test_get_saved_view_returns_v1_annotation_document_when_persisted(
        self, service_with_view: GraphService
    ):
        """Saved views can carry the v1 annotation document while keeping legacy fields."""
        storage = service_with_view._storage
        view = storage.get_node("view-1")
        view.metadata["annotation_schema_version"] = 1
        view.metadata["annotation_document"] = {
            "schema_version": 1,
            "annotations": [
                {
                    "id": "group-1",
                    "type": "group",
                    "kind": "group",
                    "label": "G",
                    "member_node_ids": ["actor-1"],
                },
                {
                    "id": "note-1",
                    "type": "note",
                    "kind": "note",
                    "text": "hello",
                    "position": {"x": 1, "y": 2},
                },
            ],
        }
        view.metadata["groups"] = [
            {"id": "group-1", "label": "G", "position": {"x": 0, "y": 0}}
        ]
        view.metadata["annotations"] = [
            {
                "id": "note-1",
                "kind": "note",
                "text": "hello",
                "position": {"x": 1, "y": 2},
            }
        ]
        storage.update_node("view-1", {"metadata": view.metadata})

        result = service_with_view.get_saved_view("Test View")

        assert result["success"] is True
        assert result["annotation_schema_version"] == 1
        assert result["annotation_document"]["annotations"][0]["type"] == "group"
        assert result["annotations"][0]["kind"] == "note"

    def test_get_saved_view_derives_legacy_fields_from_v1_only_document(
        self, service_with_view: GraphService
    ):
        """Existing load paths still receive groups/annotations from v1-only saved views."""
        storage = service_with_view._storage
        view = storage.get_node("view-1")
        view.metadata["annotation_schema_version"] = 1
        view.metadata["annotation_document"] = {
            "schema_version": 1,
            "annotations": [
                {
                    "id": "group-1",
                    "type": "group",
                    "kind": "group",
                    "label": "Styled group",
                    "description": "Team area",
                    "geometry": {"x": 10, "y": 20, "w": 320, "h": 180},
                    "style": {"color": "#f5a623"},
                    "member_node_ids": ["actor-1"],
                },
                {
                    "id": "note-1",
                    "type": "note",
                    "kind": "note",
                    "text": "hello",
                    "position": {"x": 1, "y": 2},
                },
            ],
        }
        view.metadata.pop("groups", None)
        view.metadata.pop("annotations", None)
        storage.update_node("view-1", {"metadata": view.metadata})

        result = service_with_view.get_saved_view("Test View")

        assert result["success"] is True
        assert result["groups"] == [
            {
                "id": "group-1",
                "label": "Styled group",
                "description": "Team area",
                "position": {"x": 10, "y": 20},
                "style": {"width": 320, "height": 180},
                "color": "#f5a623",
            }
        ]
        assert result["parentIds"] == {"actor-1": "group-1"}
        assert result["annotations"] == [
            {
                "id": "note-1",
                "type": "note",
                "kind": "note",
                "text": "hello",
                "position": {"x": 1, "y": 2},
            }
        ]

    def test_list_saved_views(self, service_with_view: GraphService):
        """Test listing saved views."""
        result = service_with_view.list_saved_views()

        assert result["success"] is True
        assert "views" in result
        assert len(result["views"]) >= 1
        assert any(v["name"] == "Test View" for v in result["views"])


class TestSavedViewAnnotationValidation:
    """Regression coverage for smallfix-saved-view-annotation-document-unvalidated:
    a SavedView node's annotation_document/annotations are ordinary node
    metadata, so add_nodes/update_node must apply the same image-annotation
    rule SessionStore enforces for live ops, and get_saved_view/get_node_details
    must not surface a pre-existing (or otherwise directly-injected) remote
    image URL even after it already reached storage.
    """

    _REMOTE_IMAGE_ANNOTATION = {
        "id": "img-1",
        "type": "image",
        "kind": "image",
        "position": {"x": 0, "y": 0},
        "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
        "image": {
            "url": "https://attacker.example/tracker.png",
            "width": 10,
            "height": 10,
        },
        "alt": "",
    }
    _EMBEDDED_IMAGE_ANNOTATION = {
        **_REMOTE_IMAGE_ANNOTATION,
        "image": {
            "url": (
                "data:image/webp;base64,"
                "UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="
            ),
            "width": 10,
            "height": 10,
        },
    }

    def _saved_view_node_dict(self, name: str, annotation: dict) -> dict:
        return {
            "name": name,
            "type": "SavedView",
            "metadata": {
                "node_ids": ["actor-1"],
                "positions": {"actor-1": {"x": 0, "y": 0}},
                "annotation_schema_version": 1,
                "annotation_document": {
                    "schema_version": 1,
                    "annotations": [annotation],
                },
                "annotations": [annotation],
            },
        }

    def test_add_nodes_rejects_saved_view_with_remote_image_annotation(
        self, populated_service: GraphService
    ):
        result = populated_service.add_nodes(
            nodes=[
                self._saved_view_node_dict(
                    "Malicious View", self._REMOTE_IMAGE_ANNOTATION
                )
            ],
            edges=[],
        )

        assert result["success"] is False
        assert "embedded" in result["message"]
        assert result["added_node_ids"] == []
        # Nothing was persisted: the view cannot be found afterwards.
        assert populated_service.get_saved_view("Malicious View")["success"] is False

    def test_add_nodes_accepts_saved_view_with_embedded_image_annotation(
        self, populated_service: GraphService
    ):
        result = populated_service.add_nodes(
            nodes=[
                self._saved_view_node_dict(
                    "Clean View", self._EMBEDDED_IMAGE_ANNOTATION
                )
            ],
            edges=[],
        )

        assert result["success"] is True
        assert len(result["added_node_ids"]) == 1

    def test_add_nodes_rejects_legacy_visualization_view_type_too(
        self, populated_service: GraphService
    ):
        node = self._saved_view_node_dict(
            "Legacy Malicious View", self._REMOTE_IMAGE_ANNOTATION
        )
        node["type"] = "VisualizationView"

        result = populated_service.add_nodes(nodes=[node], edges=[])

        assert result["success"] is False

    def test_add_nodes_does_not_validate_annotations_on_non_saved_view_nodes(
        self, populated_service: GraphService
    ):
        """The guard is scoped to SavedView/VisualizationView metadata -- an
        unrelated node type carrying an `annotations` key (meaningless there)
        must not be caught by it."""
        result = populated_service.add_nodes(
            nodes=[
                {
                    "type": "Actor",
                    "name": "Some Actor",
                    "metadata": {"annotations": [self._REMOTE_IMAGE_ANNOTATION]},
                }
            ],
            edges=[],
        )

        assert result["success"] is True

    def test_update_node_rejects_saved_view_with_remote_image_annotation(
        self, service_with_view: GraphService
    ):
        result = service_with_view.update_node(
            "view-1",
            {
                "metadata": {
                    "annotation_document": {
                        "schema_version": 1,
                        "annotations": [self._REMOTE_IMAGE_ANNOTATION],
                    }
                }
            },
            metadata_merge=True,
        )

        assert result["success"] is False
        assert "embedded" in result["error"]
        # The rejected write must not have applied.
        stored = service_with_view._storage.get_node("view-1")
        assert "annotation_document" not in stored.metadata

    def test_update_node_accepts_saved_view_with_embedded_image_annotation(
        self, service_with_view: GraphService
    ):
        result = service_with_view.update_node(
            "view-1",
            {
                "metadata": {
                    "annotation_document": {
                        "schema_version": 1,
                        "annotations": [self._EMBEDDED_IMAGE_ANNOTATION],
                    }
                }
            },
            metadata_merge=True,
        )

        assert result["success"] is True

    def test_update_node_ignores_updates_without_annotation_content(
        self, service_with_view: GraphService
    ):
        """A rename/description-only update on a SavedView node, carrying no
        annotation content at all, must not be caught by the guard."""
        result = service_with_view.update_node("view-1", {"description": "Renamed"})

        assert result["success"] is True

    def test_get_saved_view_sanitizes_pre_existing_remote_image_annotation(
        self, service_with_view: GraphService
    ):
        """A view whose annotation content reached storage before this guard
        existed (simulated here by writing directly through storage, bypassing
        the service-layer guard the same way the SessionStore image guard's
        own tests simulate legacy data) must not surface the remote URL when
        the view is opened."""
        storage = service_with_view._storage
        view = storage.get_node("view-1")
        view.metadata["annotation_schema_version"] = 1
        view.metadata["annotation_document"] = {
            "schema_version": 1,
            "annotations": [self._REMOTE_IMAGE_ANNOTATION],
        }
        view.metadata["annotations"] = [self._REMOTE_IMAGE_ANNOTATION]
        storage.update_node("view-1", {"metadata": view.metadata})

        result = service_with_view.get_saved_view("Test View")

        assert result["success"] is True
        doc_image = result["annotation_document"]["annotations"][0]["image"]
        assert "url" not in doc_image
        legacy_image = result["annotations"][0]["image"]
        assert "url" not in legacy_image
        # Sanitization never mutates storage, only the read-time response.
        stored = storage.get_node("view-1")
        assert (
            stored.metadata["annotation_document"]["annotations"][0]["image"]["url"]
            == self._REMOTE_IMAGE_ANNOTATION["image"]["url"]
        )

    def test_get_saved_view_leaves_embedded_image_annotation_untouched(
        self, service_with_view: GraphService
    ):
        storage = service_with_view._storage
        view = storage.get_node("view-1")
        view.metadata["annotation_document"] = {
            "schema_version": 1,
            "annotations": [self._EMBEDDED_IMAGE_ANNOTATION],
        }
        storage.update_node("view-1", {"metadata": view.metadata})

        result = service_with_view.get_saved_view("Test View")

        assert (
            result["annotation_document"]["annotations"][0]["image"]["url"]
            == self._EMBEDDED_IMAGE_ANNOTATION["image"]["url"]
        )

    def test_get_node_details_sanitizes_saved_view_remote_image_annotation(
        self, service_with_view: GraphService
    ):
        """The 'double-click a SavedView node to open it' flow
        (frontend/web/src/App.jsx) reads metadata.annotations straight off a
        generically-serialized node rather than calling get_saved_view again,
        so get_node_details must be safe on its own too."""
        storage = service_with_view._storage
        view = storage.get_node("view-1")
        view.metadata["annotations"] = [self._REMOTE_IMAGE_ANNOTATION]
        storage.update_node("view-1", {"metadata": view.metadata})

        result = service_with_view.get_node_details("view-1")

        assert result["success"] is True
        image = result["node"]["metadata"]["annotations"][0]["image"]
        assert "url" not in image

    def test_get_node_details_on_non_saved_view_node_is_unaffected(
        self, populated_service: GraphService
    ):
        """The sanitizer only inspects SavedView/VisualizationView metadata;
        an unrelated node's metadata passes through serialize_node untouched."""
        result = populated_service.add_nodes(
            nodes=[
                {
                    "type": "Actor",
                    "name": "Some Actor",
                    "metadata": {"annotations": [self._REMOTE_IMAGE_ANNOTATION]},
                }
            ],
            edges=[],
        )
        node_id = result["added_node_ids"][0]

        details = populated_service.get_node_details(node_id)

        assert (
            details["node"]["metadata"]["annotations"][0]["image"]["url"]
            == self._REMOTE_IMAGE_ANNOTATION["image"]["url"]
        )


class TestGraphServiceExport:
    """Tests for export operations."""

    def test_export_graph(self, populated_service: GraphService):
        """Test exporting the entire graph."""
        result = populated_service.export_graph()

        assert "version" in result
        assert "exportDate" in result
        assert "nodes" in result
        assert "edges" in result
        assert result["total_nodes"] == 5
        assert result["total_edges"] == 4
        # Verify all nodes are included
        node_names = [n["name"] for n in result["nodes"]]
        assert "Skatteverket" in node_names
        assert "Digital First" in node_names


class TestGraphServiceTenantContext:
    """Tests for get_tenant_context via GraphService."""

    def test_returns_expected_shape(self, empty_service: GraphService):
        """get_tenant_context returns all three required keys."""
        result = empty_service.get_tenant_context()
        assert set(result.keys()) == {"tenant_id", "tenant_name", "environment"}

    def test_defaults_when_env_unset(self, empty_service: GraphService, monkeypatch):
        """Safe defaults are returned when no env vars are set."""
        monkeypatch.delenv("COMMUNITYOVERVIEW_TENANT_ID", raising=False)
        monkeypatch.delenv("COMMUNITYOVERVIEW_TENANT_NAME", raising=False)
        monkeypatch.delenv("COMMUNITYOVERVIEW_ENVIRONMENT", raising=False)

        result = empty_service.get_tenant_context()

        assert result["tenant_id"] == ""
        assert result["tenant_name"] == ""
        assert result["environment"] == "local"

    def test_env_vars_override_defaults(self, empty_service: GraphService, monkeypatch):
        """Env vars override the defaults."""
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_ID", "org-42")
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_NAME", "Org 42")
        monkeypatch.setenv("COMMUNITYOVERVIEW_ENVIRONMENT", "production")

        result = empty_service.get_tenant_context()

        assert result == {
            "tenant_id": "org-42",
            "tenant_name": "Org 42",
            "environment": "production",
        }


class TestGraphServiceConfigContext:
    """Tests for get_config_context via GraphService."""

    def test_returns_effective_config_context(
        self, empty_service: GraphService, monkeypatch, tmp_path
    ):
        tenant_dir = tmp_path / "tenant-config"
        tenant_dir.mkdir()
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_CONFIG_DIR", str(tenant_dir))

        result = empty_service.get_config_context()

        assert result["tenant_config_dir_configured"] is True
        assert result["schema_config_source"] == "tenant_config_dir"
        assert result["federation_config_source"] == "tenant_config_dir"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result


class TestGraphServiceSerialization:
    """Tests for response serialization."""

    def test_datetime_serialization(self, populated_service: GraphService):
        """Test that datetime fields are properly serialized."""
        result = populated_service.get_node_details("actor-1")

        # created_at and updated_at should be ISO format strings
        assert isinstance(result["node"]["created_at"], str)
        assert isinstance(result["node"]["updated_at"], str)

    def test_export_datetime_serialization(self, populated_service: GraphService):
        """Test datetime serialization in export."""
        result = populated_service.export_graph()

        assert isinstance(result["exportDate"], str)
        # All node timestamps should be strings
        for node in result["nodes"]:
            assert isinstance(node["created_at"], str)
