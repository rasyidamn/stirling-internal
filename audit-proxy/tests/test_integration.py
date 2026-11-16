"""End-to-end tests against the real docker compose stack.

Run via scripts/integration-test.sh (starts stirling-pdf + audit-proxy with
compose.test.yml, which exposes audit-proxy on 127.0.0.1:18081)."""
import os
import subprocess
import time
import zipfile
import io
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader

from tests.conftest import make_pdf, read_audit

pytestmark = pytest.mark.integration

PROXY = os.environ.get("AUDIT_PROXY_URL", "http://127.0.0.1:18081")
LOG_DIR = Path(os.environ.get("AUDIT_LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))
USER = {"Remote-User": "tester", "Remote-Groups": "GG-PDF-Users",
        "Remote-Email": "tester@example.local", "X-Forwarded-For": "10.9.9.9"}


def sha256sum(path: Path) -> str:
    """Use the real sha256sum binary, as an independent check of our hashing."""
    return subprocess.run(["sha256sum", str(path)], check=True, capture_output=True,
                          text=True).stdout.split()[0]


def new_audit_lines(before: int, expect: int = 1, timeout: float = 10) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = read_audit(LOG_DIR)
        if len(lines) >= before + expect:
            return lines[before:]
        time.sleep(0.2)
    raise AssertionError(f"expected {expect} new audit line(s) in {LOG_DIR}")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=PROXY, timeout=300) as c:
        yield c


@pytest.fixture
def pdfs(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(make_pdf(2))
    b.write_bytes(make_pdf(3))
    return a, b


def test_ui_loads_through_proxy(client):
    r = client.get("/", headers=USER)
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_merge_end_to_end(client, pdfs, tmp_path):
    a, b = pdfs
    before = len(read_audit(LOG_DIR))
    r = client.post("/api/v1/general/merge-pdfs", headers=USER,
                    files=[("fileInput", ("a.pdf", a.read_bytes(), "application/pdf")),
                           ("fileInput", ("b.pdf", b.read_bytes(), "application/pdf"))],
                    data={"sortType": "orderProvided"})
    assert r.status_code == 200, r.text[:300]
    out = tmp_path / "merged.pdf"
    out.write_bytes(r.content)
    assert len(PdfReader(out).pages) == 5  # 2 + 3

    [rec] = new_audit_lines(before)
    assert rec["user"] == "tester" and rec["operation"] == "merge"
    assert rec["result"] == "success" and rec["status"] == 200
    assert rec["ip"] == "10.9.9.9"
    assert [i["sha256"] for i in rec["inputs"]] == [sha256sum(a), sha256sum(b)]
    assert [i["pages"] for i in rec["inputs"]] == [2, 3]
    assert rec["output"]["sha256"] == sha256sum(out)
    assert rec["output"]["size_bytes"] == out.stat().st_size
    assert rec["output"]["filename"].endswith(".pdf")


def test_split_end_to_end(client, pdfs, tmp_path):
    _, b = pdfs
    before = len(read_audit(LOG_DIR))
    r = client.post("/api/v1/general/split-pages", headers=USER,
                    files=[("fileInput", ("b.pdf", b.read_bytes(), "application/pdf"))],
                    data={"pageNumbers": "1"})
    assert r.status_code == 200, r.text[:300]
    out = tmp_path / "split.bin"
    out.write_bytes(r.content)
    assert zipfile.is_zipfile(io.BytesIO(r.content))
    [rec] = new_audit_lines(before)
    assert rec["operation"] == "split" and rec["output"]["sha256"] == sha256sum(out)


def test_compress_end_to_end(client, pdfs, tmp_path):
    a, _ = pdfs
    before = len(read_audit(LOG_DIR))
    r = client.post("/api/v1/misc/compress-pdf", headers=USER,
                    files=[("fileInput", ("a.pdf", a.read_bytes(), "application/pdf"))],
                    data={"optimizeLevel": "2"})
    assert r.status_code == 200, r.text[:300]
    out = tmp_path / "c.pdf"
    out.write_bytes(r.content)
    [rec] = new_audit_lines(before)
    assert rec["operation"] == "compress" and rec["inputs"][0]["sha256"] == sha256sum(a)
    assert rec["output"]["sha256"] == sha256sum(out)


def test_rotate_end_to_end(client, pdfs, tmp_path):
    a, _ = pdfs
    before = len(read_audit(LOG_DIR))
    r = client.post("/api/v1/general/rotate-pdf", headers=USER,
                    files=[("fileInput", ("a.pdf", a.read_bytes(), "application/pdf"))],
                    data={"angle": "90"})
    assert r.status_code == 200, r.text[:300]
    reader = PdfReader(io.BytesIO(r.content))
    assert reader.pages[0].rotation == 90
    [rec] = new_audit_lines(before)
    assert rec["operation"] == "rotate" and rec["params"]["angle"] == "90"


def test_convert_pdf_to_img_end_to_end(client, pdfs):
    a, _ = pdfs
    before = len(read_audit(LOG_DIR))
    r = client.post("/api/v1/convert/pdf/img", headers=USER,
                    files=[("fileInput", ("a.pdf", a.read_bytes(), "application/pdf"))],
                    data={"imageFormat": "png", "singleOrMultiple": "single", "dpi": "72",
                          "colorType": "color"})
    assert r.status_code == 200, r.text[:300]
    [rec] = new_audit_lines(before)
    assert rec["operation"] == "convert:pdf-to-img" and rec["result"] == "success"


def test_password_is_not_logged(client, pdfs):
    a, _ = pdfs
    before = len(read_audit(LOG_DIR))
    r = client.post("/api/v1/security/add-password", headers=USER,
                    files=[("fileInput", ("a.pdf", a.read_bytes(), "application/pdf"))],
                    data={"password": "SangatRahasia!42", "keyLength": "256"})
    assert r.status_code == 200, r.text[:300]
    [rec] = new_audit_lines(before)
    assert rec["params"]["password"] == "***"
    assert "SangatRahasia!42" not in (LOG_DIR / "audit.jsonl").read_text()


def test_rejects_without_remote_user(client, pdfs):
    a, _ = pdfs
    before = len(read_audit(LOG_DIR))
    r = client.post("/api/v1/general/merge-pdfs",
                    files=[("fileInput", ("a.pdf", a.read_bytes(), "application/pdf"))])
    assert r.status_code == 401
    [rec] = new_audit_lines(before)
    assert rec["result"] == "rejected" and rec["user"] is None


def test_blocked_admin_api(client):
    assert client.get("/swagger-ui/index.html", headers=USER).status_code == 403
    assert client.post("/api/v1/admin/settings", headers=USER, json={}).status_code == 403


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["log_ok"] is True
