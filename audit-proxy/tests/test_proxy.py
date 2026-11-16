"""Proxy behaviour against a fake Stirling (httpx.MockTransport)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import make_pdf, read_audit, sha

AUTH = {"Remote-User": "budi", "Remote-Groups": "GG-PDF-Users,Staff",
        "Remote-Email": "budi@example.local", "Remote-Name": "Budi Santoso",
        "X-Forwarded-For": "10.0.5.23"}
MERGED = make_pdf(5)


class FakeStirling:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = request.read()
        self.bodies.append(body)
        p = request.url.path
        if p == "/api/v1/general/merge-pdfs":
            if request.url.params.get("async") == "true":
                return httpx.Response(200, json={"async": True, "jobId": "job-42", "result": None})
            return httpx.Response(200, content=MERGED, headers={
                "content-type": "application/pdf",
                "content-disposition": 'form-data; name="attachment"; filename="a_merged.pdf"'})
        if p == "/api/v1/misc/compress-pdf":
            return httpx.Response(500, json={"error": "boom"})
        if p == "/api/v1/general/job/job-42/result":
            return httpx.Response(200, content=MERGED, headers={"content-type": "application/pdf"})
        if p == "/redirect":
            return httpx.Response(302, headers={"location": "http://stirling-pdf:8080/target"})
        if p == "/":
            return httpx.Response(200, text="<html>ui</html>", headers={
                "content-type": "text/html", "set-cookie": "a=1"})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    fake = FakeStirling()
    settings = Settings(upstream_url="http://stirling-pdf:8080", log_dir=str(tmp_path),
                        max_upload_mb=1, tmp_dir=str(tmp_path))
    app = create_app(settings, transport=httpx.MockTransport(fake))
    with TestClient(app) as client:
        yield client, fake, tmp_path


def merge_files(*pdfs):
    return [("fileInput", (f"f{i}.pdf", p, "application/pdf")) for i, p in enumerate(pdfs)]


def test_merge_is_forwarded_and_audited(env, pdf2, pdf3):
    client, fake, logs = env
    r = client.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2, pdf3),
                    data={"sortType": "orderProvided"})
    assert r.status_code == 200
    assert r.content == MERGED
    # body reached Stirling byte-for-byte with the original boundary
    assert pdf2 in fake.bodies[0] and pdf3 in fake.bodies[0]
    assert fake.requests[0].headers["accept-encoding"] == "identity"

    [rec] = read_audit(logs)
    assert rec["user"] == "budi" and rec["name"] == "Budi Santoso"
    assert rec["groups"] == ["GG-PDF-Users", "Staff"]
    assert rec["email"] == "budi@example.local"
    assert rec["ip"] == "10.0.5.23"
    assert rec["endpoint"] == "POST /api/v1/general/merge-pdfs"
    assert rec["operation"] == "merge"
    assert rec["params"] == {"sortType": "orderProvided"}
    assert [(i["sha256"], i["pages"]) for i in rec["inputs"]] == [(sha(pdf2), 2), (sha(pdf3), 3)]
    assert rec["status"] == 200 and rec["result"] == "success"
    assert rec["output"] == {"filename": "a_merged.pdf", "content_type": "application/pdf",
                             "size_bytes": len(MERGED), "sha256": sha(MERGED)}
    assert rec["ts"].endswith("+07:00")
    assert rec["duration_ms"] >= 0 and rec["error"] is None


def test_missing_remote_user_rejected(env, pdf2):
    client, fake, logs = env
    r = client.post("/api/v1/general/merge-pdfs", files=merge_files(pdf2))
    assert r.status_code == 401
    assert fake.requests == []
    [rec] = read_audit(logs)
    assert rec["result"] == "rejected" and rec["user"] is None and rec["status"] == 401


def test_missing_remote_user_on_ui_not_audited(env):
    client, fake, logs = env
    assert client.get("/").status_code == 401
    assert read_audit(logs) == []


def test_empty_remote_user_rejected(env):
    client, _, _ = env
    assert client.get("/", headers={"Remote-User": ""}).status_code == 401


def test_blocked_endpoint(env):
    client, fake, logs = env
    r = client.post("/api/v1/admin/settings", headers=AUTH, json={})
    assert r.status_code == 403 and fake.requests == []
    assert client.get("/swagger-ui/index.html", headers=AUTH).status_code == 403
    recs = read_audit(logs)
    assert [r["result"] for r in recs] == ["rejected", "rejected"]


def test_group_check(tmp_path, pdf2):
    fake = FakeStirling()
    s = Settings(log_dir=str(tmp_path), allowed_groups=("gg-pdf-users",))
    with TestClient(create_app(s, transport=httpx.MockTransport(fake))) as c:
        assert c.get("/", headers=AUTH).status_code == 200  # case-insensitive match
        other = {**AUTH, "Remote-Groups": "Finance"}
        assert c.get("/", headers=other).status_code == 403
        assert c.get("/", headers={"Remote-User": "x"}).status_code == 403


def test_upload_limit_by_content_length(env):
    client, fake, logs = env
    big = b"%PDF" + b"0" * (1024 * 1024 + 10)
    r = client.post("/api/v1/general/merge-pdfs", headers=AUTH,
                    files=[("fileInput", ("big.pdf", big, "application/pdf"))])
    assert r.status_code == 413 and "200" not in r.json()["error"] and "1 MB" in r.json()["error"]
    assert fake.requests == []
    [rec] = read_audit(logs)
    assert rec["result"] == "rejected" and rec["status"] == 413


def test_upload_limit_when_streamed_without_length(env):
    client, fake, logs = env
    boundary = "zzz"
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileInput\"; "
               f"filename=\"x.pdf\"\r\n\r\n").encode() + b"0" * (1024 * 1024 + 10) + \
        f"\r\n--{boundary}--\r\n".encode()

    def gen():
        for i in range(0, len(payload), 65536):
            yield payload[i:i + 65536]

    r = client.post("/api/v1/general/merge-pdfs", content=gen(),
                    headers={**AUTH, "content-type": f"multipart/form-data; boundary={boundary}"})
    assert r.status_code == 413
    assert fake.requests == []
    assert read_audit(logs)[0]["result"] == "rejected"


def test_upstream_error_audited(env, pdf2):
    client, _, logs = env
    r = client.post("/api/v1/misc/compress-pdf", headers=AUTH, files=merge_files(pdf2))
    assert r.status_code == 500
    [rec] = read_audit(logs)
    assert rec["result"] == "error" and rec["status"] == 500
    assert rec["operation"] == "compress" and "boom" in rec["error"]
    assert rec["output"] is None and rec["inputs"][0]["sha256"] == sha(pdf2)


def test_upstream_down_audited(tmp_path, pdf2):
    def down(request):
        raise httpx.ConnectError("refused")
    s = Settings(log_dir=str(tmp_path))
    with TestClient(create_app(s, transport=httpx.MockTransport(down))) as c:
        r = c.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2))
        assert r.status_code == 502
        assert c.get("/", headers=AUTH).status_code == 502
    [rec] = read_audit(tmp_path)
    assert rec["result"] == "error" and rec["status"] == 502


def test_async_jobs_blocked(env, pdf2):
    client, fake, logs = env
    r = client.post("/api/v1/general/merge-pdfs?async=true", headers=AUTH, files=merge_files(pdf2))
    assert r.status_code == 403
    r = client.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2),
                    data={"async": "TRUE"})            # as a form field too
    assert r.status_code == 403
    assert client.get("/api/v1/general/job/job-42/result", headers=AUTH).status_code == 403
    assert fake.requests == []
    recs = read_audit(logs)
    assert [x["result"] for x in recs] == ["rejected"] * 3
    assert recs[1]["inputs"][0]["sha256"] == sha(pdf2)   # still know what was attempted


@pytest.mark.parametrize("path", [
    "/api/v1/admin;x=1/settings",
    "/api/v1//admin/settings",
    "/api/v1/admin;/settings",
    "/api/v1/general/../admin/settings",
    "/api/v1/brand-new-admin-api/do",
    "/API/V1/ADMIN/settings",
])
def test_path_tricks_blocked(env, path):
    client, fake, logs = env
    r = client.post(path, headers=AUTH, json={})
    assert r.status_code in (400, 403)
    assert fake.requests == []
    [rec] = read_audit(logs)
    assert rec["result"] == "rejected"


def test_forwarded_path_is_normalized(env):
    client, fake, _ = env
    assert client.get("/api//v1/config;x/app-config", headers=AUTH).status_code == 404
    assert fake.requests[0].url.path == "/api/v1/config/app-config"


def test_non_multipart_post_is_audited(env):
    client, fake, logs = env
    r = client.post("/api/v1/general/merge-pdfs", headers=AUTH,
                    data={"fileId": "abc", "password": "rahasia"})  # urlencoded
    assert r.status_code == 200 and len(fake.requests) == 1
    [rec] = read_audit(logs)
    assert rec["result"] == "success" and rec["inputs"] == []
    assert rec["params"] == {"fileId": "abc", "password": "***"}
    assert rec["request_body"]["content_type"].startswith("application/x-www-form-urlencoded")
    assert rec["request_body"]["size_bytes"] > 0 and len(rec["request_body"]["sha256"]) == 64
    assert "rahasia" not in (logs / "audit.jsonl").read_text()


def test_json_post_is_audited_without_content(env):
    client, _, logs = env
    client.post("/api/v1/misc/compress-pdf", headers=AUTH, json={"secretThing": "x" * 50})
    [rec] = read_audit(logs)
    assert rec["request_body"]["content_type"] == "application/json"
    assert rec["params"] == {} and "xxxxx" not in (logs / "audit.jsonl").read_text()


def test_ui_meter_not_audited(env):
    client, _, logs = env
    client.post("/api/v1/automation/meter", headers=AUTH, json={"n": 1})
    assert read_audit(logs) == []


def test_spool_failure_is_audited(env, pdf2, monkeypatch):
    client, fake, logs = env
    import app.main as main_mod

    def full(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(main_mod.tempfile, "TemporaryFile", full)
    r = client.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2))
    assert r.status_code == 507 and "penuh" in r.json()["error"]
    assert fake.requests == []
    [rec] = read_audit(logs)
    assert rec["result"] == "error" and rec["status"] == 507 and "No space" in rec["error"]


def test_concurrency_limit(tmp_path, pdf2):
    s = Settings(log_dir=str(tmp_path), max_concurrent_uploads=1, upload_queue_timeout_s=0)
    with TestClient(create_app(s, transport=httpx.MockTransport(FakeStirling()))) as c:
        sem = c.app.state.upload_sem
        c.portal.call(sem.acquire)          # simulate one upload in flight
        r = c.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2))
        assert r.status_code == 503 and "sibuk" in r.json()["error"]
        sem.release()
        assert c.post("/api/v1/general/merge-pdfs", headers=AUTH,
                      files=merge_files(pdf2)).status_code == 200
    busy, done = read_audit(tmp_path)
    assert busy["result"] == "rejected" and busy["status"] == 503
    assert done["result"] == "success"


def test_page_count_skipped_for_large_files(tmp_path, pdf2):
    s = Settings(log_dir=str(tmp_path), page_count_max_mb=0)
    with TestClient(create_app(s, transport=httpx.MockTransport(FakeStirling()))) as c:
        c.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2))
    [rec] = read_audit(tmp_path)
    assert rec["inputs"][0]["pages"] is None and rec["inputs"][0]["sha256"] == sha(pdf2)


def test_fail_closed(tmp_path, pdf2):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    s = Settings(log_dir=str(blocker), audit_fail_closed=True)
    fake = FakeStirling()
    with TestClient(create_app(s, transport=httpx.MockTransport(fake))) as c:
        # first call discovers the broken log (logger.ok starts True) ...
        c.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2))
        # ... then operations are refused, while the UI keeps working
        r = c.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2))
        assert r.status_code == 503
        assert c.get("/", headers=AUTH).status_code == 200
    assert len(fake.requests) == 2  # first merge + UI page


def test_passthrough_not_audited(env):
    client, fake, logs = env
    r = client.get("/?lang=id", headers=AUTH)
    assert r.status_code == 200 and r.text == "<html>ui</html>"
    assert r.headers["set-cookie"] == "a=1"
    assert fake.requests[0].url.query == b"lang=id"
    assert "host" not in {k for k in fake.requests[0].headers if k == "host" and
                          fake.requests[0].headers[k] == "testserver"}
    assert read_audit(logs) == []


def test_redirect_location_rewritten(env):
    client, _, _ = env
    r = client.get("/redirect", headers=AUTH, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/target"


def test_healthz_no_auth_no_audit(env):
    client, _, logs = env
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["log_ok"] is True
    assert "log_path" in r.json()          # in-container check: full details
    via_caddy = client.get("/healthz", headers={"X-Forwarded-For": "10.0.5.23"})
    assert via_caddy.json() == {"status": "ok", "log_ok": True}
    assert read_audit(logs) == []


def test_log_failure_does_not_break_request(tmp_path, pdf2):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # log_dir is a file -> every write fails
    s = Settings(log_dir=str(blocker))
    with TestClient(create_app(s, transport=httpx.MockTransport(FakeStirling()))) as c:
        r = c.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2))
        assert r.status_code == 200 and r.content == MERGED
        h = c.get("/healthz")
        assert h.status_code == 503
        assert h.json()["log_ok"] is False and h.json()["log_errors"] >= 1


def test_spoofed_xff_uses_rightmost(env):
    client, _, logs = env
    client.post("/api/v1/misc/compress-pdf", files=merge_files(make_pdf(1)),
                headers={**AUTH, "X-Forwarded-For": "1.2.3.4, 10.0.5.99"})
    assert read_audit(logs)[0]["ip"] == "10.0.5.99"


def test_audit_line_is_valid_single_json(env, pdf2):
    client, _, logs = env
    client.post("/api/v1/general/merge-pdfs", headers=AUTH, files=merge_files(pdf2),
                data={"password": "rahasia"})
    raw = (logs / "audit.jsonl").read_text()
    assert raw.count("\n") == 1 and "rahasia" not in raw
    assert json.loads(raw)["params"]["password"] == "***"
