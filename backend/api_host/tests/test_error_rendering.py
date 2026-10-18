"""The hosted app renders 4xx bodies that echo a lone surrogate instead of 500."""

from fastapi.exceptions import RequestValidationError

from backend.service.json_response import GraphJSONResponse


def test_app_keeps_its_conflict_status_when_the_error_detail_holds_a_lone_surrogate(
    test_app_empty_graph,
):
    node = '{"nodes":[{"id":"dup\\ud800","type":"Actor","name":"A"}],"edges":[]'
    headers = {"content-type": "application/json"}
    first = test_app_empty_graph.post("/api/nodes", content=node + "}", headers=headers)
    assert first.status_code == 200

    response = test_app_empty_graph.post(
        "/api/nodes", content=node + ',"expect_absent":true}', headers=headers
    )

    assert response.status_code == 409


def test_app_renders_errors_with_the_graph_response_class(test_app_empty_graph):
    app = test_app_empty_graph.app
    handler = app.exception_handlers[RequestValidationError]

    assert GraphJSONResponse.__name__ in handler.__code__.co_names
