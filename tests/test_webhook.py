import json

import pytest
from fastapi.testclient import TestClient

from prharness.main import create_app
from tests.conftest import RecordingHandler


@pytest.fixture
def recorder(settings):
    return RecordingHandler(settings)


@pytest.fixture
def client(settings, recorder):
    with TestClient(create_app(settings, handlers=[recorder])) as c:
        yield c


def post(client, body: bytes, headers: dict):
    return client.post("/webhook", content=body, headers=headers)


def test_health(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_pr_opened_is_dispatched(client, recorder, sample_payload, signed_headers) -> None:
    body = json.dumps(sample_payload).encode()
    response = post(client, body, signed_headers(body, delivery="abc"))
    assert response.status_code == 202
    assert response.json() == {"status": "dispatched", "handlers": ["recording"]}
    [event] = recorder.events
    assert (event.repo_full_name, event.number, event.action) == (
        "octo-org/octo-repo",
        42,
        "opened",
    )
    assert (event.author, event.head_ref, event.base_ref) == ("octocat", "feature/widgets", "main")
    assert event.installation_id == 123456 and event.delivery_id == "abc"


def test_bad_signature_rejected(client, recorder, sample_payload, signed_headers) -> None:
    body = json.dumps(sample_payload).encode()
    headers = signed_headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert post(client, body, headers).status_code == 401
    assert recorder.events == []


def test_duplicate_delivery_ignored(client, recorder, sample_payload, signed_headers) -> None:
    body = json.dumps(sample_payload).encode()
    post(client, body, signed_headers(body, delivery="same"))
    response = post(client, body, signed_headers(body, delivery="same"))
    assert response.json()["reason"] == "duplicate delivery"
    assert len(recorder.events) == 1


def test_ping_and_other_events(client, recorder, signed_headers) -> None:
    body = b"{}"
    assert post(client, body, signed_headers(body, event="ping")).json() == {"status": "pong"}
    assert post(client, body, signed_headers(body, event="push")).json()["status"] == "ignored"
    assert recorder.events == []


def test_unconfigured_action_ignored(client, recorder, sample_payload, signed_headers) -> None:
    sample_payload["action"] = "closed"
    body = json.dumps(sample_payload).encode()
    assert post(client, body, signed_headers(body)).json()["reason"] == "action=closed"
    assert recorder.events == []


def test_wildcard_actions_and_ignore_drafts(settings, sample_payload, signed_headers) -> None:
    settings.pr_actions = "*"
    settings.ignore_drafts = True
    recorder = RecordingHandler(settings)
    with TestClient(create_app(settings, handlers=[recorder])) as client:
        sample_payload["action"] = "synchronize"
        body = json.dumps(sample_payload).encode()
        assert post(client, body, signed_headers(body)).json()["status"] == "dispatched"
        sample_payload["pull_request"]["draft"] = True
        body = json.dumps(sample_payload).encode()
        assert post(client, body, signed_headers(body)).json()["reason"] == "draft"
    assert len(recorder.events) == 1


def test_malformed_payload(client, signed_headers) -> None:
    body = b"not json"
    assert post(client, body, signed_headers(body)).status_code == 400
    body = json.dumps({"action": "opened"}).encode()
    assert post(client, body, signed_headers(body)).status_code == 400


def test_payload_too_large(settings, signed_headers) -> None:
    settings.max_webhook_bytes = 10
    with TestClient(create_app(settings, handlers=[])) as client:
        body = b"x" * 100
        assert post(client, body, signed_headers(body)).status_code == 413


def test_startup_requires_secret(settings) -> None:
    settings.github_webhook_secret = ""
    with pytest.raises(RuntimeError), TestClient(create_app(settings, handlers=[])):
        pass
