"""The webhook receiver: signature, size cap, routing, org filter and store writes."""

import hashlib
import hmac
import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webhook"))
import receiver  # noqa: E402

SECRET = "s3cret"


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def job_body(job_id=1, org="example-org"):
    job = {"id": job_id, "run_id": 10, "run_attempt": 1, "status": "queued", "created_at": "2026-10-04T10:00:00Z"}
    repo = {"full_name": org + "/r", "owner": {"login": org}}
    return json.dumps({"action": "queued", "workflow_job": job, "repository": repo}).encode()


@pytest.fixture
def srv(tmp_path):
    store = receiver.ci_store.Store(tmp_path / "ci.db")
    s = receiver.Receiver(("127.0.0.1", 0), store, SECRET, "example-org")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()
    store.close()


def req(srv, method="POST", path="/webhook", body=b"", headers=None, sign_it=True, event="workflow_job", delivery="d1"):
    h = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if sign_it and method == "POST":
        h["X-Hub-Signature-256"] = sign(body)
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request(method, path, body=body if method == "POST" else None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_valid_signature_stores_job(srv):
    status, data = req(srv, body=job_body())
    assert status == 200 and b"stored" in data
    assert [j["id"] for j in srv.store.jobs()] == [1]


@pytest.mark.parametrize("headers", [{}, {"X-Hub-Signature-256": "sha256=" + "0" * 64}, {"X-Hub-Signature-256": "x"}])
def test_bad_or_missing_signature(srv, headers):
    status, data = req(srv, body=job_body(), sign_it=False, headers=headers)
    assert (status, data) == (401, b"")
    assert srv.store.jobs() == []


def test_wrong_secret_rejected(srv):
    body = job_body()
    assert req(srv, body=body, headers={"X-Hub-Signature-256": sign(body, "other")})[0] == 401


def test_oversize_413(srv):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.putrequest("POST", "/webhook")
    c.putheader("Content-Length", str(receiver.MAX_BODY + 1))
    c.endheaders()
    r = c.getresponse()
    assert (r.status, r.read()) == (413, b"")
    c.close()


def test_missing_content_length_411(srv):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.putrequest("POST", "/webhook")
    c.endheaders()
    assert c.getresponse().status == 411
    c.close()


def test_paths_and_methods(srv):
    assert req(srv, path="/other", body=b"{}")[0] == 404
    assert req(srv, method="GET", path="/webhook") == (405, b"")
    assert req(srv, method="GET", path="/") == (404, b"")
    assert req(srv, method="PUT", path="/healthz") == (404, b"")


def test_wrong_org_ignored(srv):
    status, data = req(srv, body=job_body(org="Someone-Else"))
    assert status == 202 and b"ignored" in data
    assert srv.store.jobs() == []


def test_duplicate_delivery(srv):
    body = job_body()
    assert b"stored" in req(srv, body=body, delivery="dup")[1]
    assert b"duplicate" in req(srv, body=body, delivery="dup")[1]
    assert len(srv.store.jobs()) == 1


def test_ping_and_unknown_event(srv):
    assert req(srv, body=b'{"zen":"x"}', event="ping")[0] == 200
    assert req(srv, body=b"{}", event="push")[0] == 202


def test_bad_json_400(srv):
    assert req(srv, body=b"not json")[0] == 400


def test_empty_secret_refuses_to_start(tmp_path, monkeypatch):
    f = tmp_path / "secret"
    f.write_text("\n")
    monkeypatch.setenv("WEBHOOK_SECRET_FILE", str(f))
    monkeypatch.setenv("CI_DB", str(tmp_path / "ci.db"))
    assert receiver.main([]) == 1


def test_healthcheck(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB", str(tmp_path / "ci.db"))
    assert receiver.main(["--healthcheck"]) == 0
    monkeypatch.setenv("CI_DB", str(tmp_path / "missing" / "ci.db"))
    assert receiver.main(["--healthcheck"]) == 1
