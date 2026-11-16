"""Full-stack tests: browser -> Caddy -> Authelia -> audit-proxy -> Stirling.

Needs the whole stack running in TEST MODE (AUTH_BACKEND=file):
    docker compose up -d --wait
    cd audit-proxy && uv run pytest -m e2e
"""
import io
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from pypdf import PdfReader

from tests.conftest import make_pdf, read_audit, sha

pytestmark = pytest.mark.e2e

HOST = os.environ.get("E2E_HOST", "pdftools.local")
TARGET_IP = os.environ.get("E2E_IP", "127.0.0.1")
BASE = f"https://{HOST}"
LOG_DIR = Path(os.environ.get("AUDIT_LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))
STAFF = ("staf.uji", "Uji-PDF-staf-2026")
OUTSIDER = ("orang.luar", "Uji-PDF-luar-2026")


@pytest.fixture(autouse=True, scope="module")
def resolve_host():
    """Point HOST at the local stack without touching the hosts file."""
    real = socket.getaddrinfo

    def fake(host, *a, **kw):
        return real(TARGET_IP if host == HOST else host, *a, **kw)

    socket.getaddrinfo = fake
    yield
    socket.getaddrinfo = real


@contextmanager
def login(user: tuple[str, str]) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE, verify=False, timeout=300, follow_redirects=False) as c:
        r = c.post("/auth/api/firstfactor", json={
            "username": user[0], "password": user[1], "keepMeLoggedIn": False,
            "targetURL": f"{BASE}/"})
        assert r.status_code == 200 and r.json()["status"] == "OK", r.text
        yield c


def last_audit(before: int, timeout: float = 10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = read_audit(LOG_DIR)
        if len(lines) > before:
            return lines[-1]
        time.sleep(0.2)
    raise AssertionError("no new audit line")


def test_http_redirects_to_https():
    with httpx.Client(verify=False) as c:
        r = c.get(f"http://{HOST}/", follow_redirects=False)
    assert r.status_code in (301, 308) and r.headers["location"].startswith("https://")


def test_anonymous_is_sent_to_login():
    with httpx.Client(verify=False, follow_redirects=False) as c:
        r = c.get(f"{BASE}/")
    assert r.status_code == 302 and "/auth/" in r.headers["location"]


def test_forged_remote_user_without_session_is_refused():
    with httpx.Client(verify=False, follow_redirects=False) as c:
        r = c.post(f"{BASE}/api/v1/general/merge-pdfs",
                   headers={"Remote-User": "direktur", "Remote-Groups": "GG-PDF-Users"},
                   files=[("fileInput", ("a.pdf", make_pdf(1), "application/pdf"))])
    assert r.status_code in (302, 303, 401)


def test_wrong_password_rejected():
    with httpx.Client(base_url=BASE, verify=False) as c:
        r = c.post("/auth/api/firstfactor", json={"username": STAFF[0], "password": "salah"})
    assert r.status_code == 401


def test_outsider_group_forbidden():
    with login(OUTSIDER) as c:
        assert c.get("/").status_code == 403


def test_staff_sees_ui():
    with login(STAFF) as c:
        r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_staff_merge_is_audited_as_real_user_even_with_forged_header():
    a, b = make_pdf(2), make_pdf(3)
    before = len(read_audit(LOG_DIR))
    with login(STAFF) as c:
        r = c.post("/api/v1/general/merge-pdfs",
                   headers={"Remote-User": "direktur", "Remote-Groups": "Domain Admins"},
                   files=[("fileInput", ("a.pdf", a, "application/pdf")),
                          ("fileInput", ("b.pdf", b, "application/pdf"))],
                   data={"sortType": "orderProvided"})
    assert r.status_code == 200
    assert len(PdfReader(io.BytesIO(r.content)).pages) == 5
    rec = last_audit(before)
    assert rec["user"] == "staf.uji" and rec["groups"] == ["GG-PDF-Users"]
    assert rec["name"] == "Staf Uji" and rec["email"] == "staf.uji@pdftools.local"
    assert [i["sha256"] for i in rec["inputs"]] == [sha(a), sha(b)]
    assert rec["output"]["sha256"] == sha(r.content)


def test_admin_api_blocked_for_staff():
    with login(STAFF) as c:
        assert c.get("/swagger-ui/index.html").status_code == 403


def test_healthz_public():
    with httpx.Client(verify=False) as c:
        r = c.get(f"{BASE}/healthz")
    assert r.status_code == 200 and r.json()["log_ok"] is True
