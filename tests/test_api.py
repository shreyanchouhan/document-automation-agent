import os

import pytest
from fastapi.testclient import TestClient

from docagent import api


@pytest.fixture(scope="module")
def client(config, corpus_directory, tmp_path_factory):
    # Its own database: these tests post and resolve, and the pipeline tests
    # assert on counts in the shared one.
    previous = os.environ.get("DOCAGENT_DB_PATH")
    os.environ["DOCAGENT_DB_PATH"] = str(tmp_path_factory.mktemp("api-state") / "docagent.db")
    api.get_pipeline.cache_clear()
    with TestClient(api.app) as test_client:
        yield test_client
    api.get_pipeline.cache_clear()
    if previous is None:
        os.environ.pop("DOCAGENT_DB_PATH", None)
    else:
        os.environ["DOCAGENT_DB_PATH"] = previous


def test_health_and_readiness(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"


def test_processing_a_batch_returns_a_summary(client):
    response = client.post("/v1/batches/batch-01/process")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["documents"] > 0
    assert set(body["summary"]) >= {"auto_post", "needs_review", "rejected", "handling_time_speedup"}


def test_an_unknown_batch_is_a_404(client):
    assert client.post("/v1/batches/batch-99/process").status_code == 404


def test_document_detail_exposes_fields_rules_and_routing(client):
    document_id = client.get("/v1/review?limit=1").json()["items"][0]["document_id"]
    body = client.get(f"/v1/documents/{document_id}").json()
    assert body["extraction"]["fields"]["total_amount"]
    assert body["validation"]["outcomes"]
    assert body["decision"]["action"] == "needs_review"


def test_resolving_a_review_removes_it_from_the_queue(client):
    first = client.get("/v1/review?limit=1").json()["items"][0]["document_id"]
    before = client.get("/v1/review?limit=1000").json()["open"]
    response = client.post(
        f"/v1/review/{first}/resolve",
        json={"resolution": "approved", "reviewer": "tester", "seconds_spent": 30},
    )
    assert response.status_code == 200
    assert client.get("/v1/review?limit=1000").json()["open"] == before - 1


def test_an_invalid_resolution_is_rejected(client):
    assert client.post(
        "/v1/review/DOC-0001/resolve", json={"resolution": "maybe", "reviewer": "tester"}
    ).status_code == 422


def test_prometheus_metrics_are_plain_text(client):
    body = client.get("/metrics").text
    assert body.startswith("# HELP")
    assert "docagent_auto_post_rate" in body
    assert "docagent_review_queue_open" in body
