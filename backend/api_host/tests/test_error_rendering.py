"""The hosted app renders 4xx bodies that echo a lone surrogate instead of 500."""

HEADERS = {"content-type": "application/json"}


def test_app_keeps_its_conflict_status_when_the_error_detail_holds_a_lone_surrogate(
    test_app_empty_graph,
):
    node = '{"nodes":[{"id":"dup\\ud800","type":"Actor","name":"A"}],"edges":[]'
    first = test_app_empty_graph.post("/api/nodes", content=node + "}", headers=HEADERS)
    assert first.status_code == 200

    response = test_app_empty_graph.post(
        "/api/nodes", content=node + ',"expect_absent":true}', headers=HEADERS
    )

    assert response.status_code == 409


def test_app_keeps_422_when_the_rejected_input_holds_a_lone_surrogate(
    test_app_empty_graph,
):
    response = test_app_empty_graph.post(
        "/api/nodes", content='{"nodes":"bad\\ud800","edges":[]}', headers=HEADERS
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["input"] == "bad\ud800"
