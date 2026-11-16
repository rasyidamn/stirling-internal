import pytest

from app.config import DEFAULT_ALLOWED_API_PREFIXES, DEFAULT_BLOCKED_PREFIXES, Settings
from app.main import parse_content_disposition_filename
from app.operations import (
    is_allowed_api,
    is_audited,
    is_blocked,
    is_sensitive_field,
    is_truthy,
    normalize_path,
    operation_for,
)


@pytest.mark.parametrize("path,op", [
    ("/api/v1/general/merge-pdfs", "merge"),
    ("/api/v1/general/split-pages", "split"),
    ("/api/v1/general/split-by-size-or-count", "split"),
    ("/api/v1/misc/compress-pdf", "compress"),
    ("/api/v1/general/rotate-pdf", "rotate"),
    ("/api/v1/convert/pdf/img", "convert:pdf-to-img"),
    ("/api/v1/convert/file/pdf", "convert:file-to-pdf"),
    ("/api/v1/security/add-password", "add-password"),
    ("/api/v1/misc/flatten", "flatten"),
    ("/api/v1/misc/repair-pdf", "repair"),
])
def test_operation_mapping(path, op):
    assert operation_for(path) == op


@pytest.mark.parametrize("raw,expected", [
    ("/", "/"),
    ("/api/v1/general/merge-pdfs", "/api/v1/general/merge-pdfs"),
    ("/api/v1/admin;x=1/settings", "/api/v1/admin/settings"),   # matrix params dropped
    ("/api/v1;a/admin;b/settings;c", "/api/v1/admin/settings"),
    ("/api//v1///admin/settings", "/api/v1/admin/settings"),    # empty segments collapsed
    ("/assets/index.js", "/assets/index.js"),
    ("/some/dir/", "/some/dir/"),                               # trailing slash kept
    ("/api/v1/general/../admin/settings", None),                # dot segments refused
    ("/api/v1/./admin", None),
    ("/api/v1/%2e%2e/admin", None),                             # double-encoded leftovers
    ("/api\\v1\\admin", None),
    ("no-leading-slash", None),
])
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/api/v1/general/merge-pdfs", True),
    ("POST", "/api/v1/general/some-new-tool", True),     # any body method, any content type
    ("PUT", "/api/v1/policies/x", True),
    ("DELETE", "/api/v1/notifications/1", True),
    ("POST", "/api/v1/automation/meter", False),         # UI-internal, exempt
    ("GET", "/api/v1/config/app-config", False),
    ("GET", "/api/v1/general/job/1/result", False),
    ("POST", "/login", False),
])
def test_is_audited(method, path, expected):
    assert is_audited(method, path, "application/json") is expected


@pytest.mark.parametrize("path,blocked", [
    ("/api/v1/admin/settings", True),
    ("/api/v1/database/import-database", True),
    ("/swagger-ui/index.html", True),
    ("/v1/api-docs", True),
    ("/api/v1/user", True),
    ("/api/v1/general/job/abc/result", True),
    ("/api/v1/users-something", False),  # prefix must end at a path boundary
    ("/api/v1/general/merge-pdfs", False),
    ("/api/v1/config/app-config", False),
])
def test_is_blocked(path, blocked):
    assert is_blocked(path, DEFAULT_BLOCKED_PREFIXES) is blocked


@pytest.mark.parametrize("path,allowed", [
    ("/api/v1/general/merge-pdfs", True),
    ("/api/v1/convert/pdf/img", True),
    ("/api/v1/config/app-config", True),
    ("/api/v1/automation/meter", True),
    ("/api/v1/automation/something-else", False),
    ("/api/v1/brand-new-admin-api/x", False),   # unknown API: closed by default
    ("/API/V1/GENERAL/merge-pdfs", False),      # case-sensitive, like Spring
    ("/api/v2/general/merge-pdfs", False),
    ("/", True),                                # non-API (UI) paths are not affected
    ("/assets/app.js", True),
])
def test_is_allowed_api(path, allowed):
    assert is_allowed_api(path, DEFAULT_ALLOWED_API_PREFIXES) is allowed


def test_allowlist_disabled():
    assert is_allowed_api("/api/v1/anything", ()) is True


def test_allowlist_from_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_API_PREFIXES", "*")
    assert Settings.from_env().allowed_api_prefixes == ()
    monkeypatch.setenv("ALLOWED_API_PREFIXES", "/api/v1/general, /api/v1/misc")
    assert Settings.from_env().allowed_api_prefixes == ("/api/v1/general", "/api/v1/misc")
    monkeypatch.delenv("ALLOWED_API_PREFIXES")
    assert Settings.from_env().allowed_api_prefixes == DEFAULT_ALLOWED_API_PREFIXES


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("on", True), (["false", "true"], True),
    ("false", False), ("", False), ([], False), (None, False),
])
def test_is_truthy(value, expected):
    assert is_truthy(value) is expected


@pytest.mark.parametrize("name,sensitive", [
    ("password", True), ("ownerPassword", True), ("retainedOpeningPassword", True),
    ("api_key", True), ("token", True), ("sortType", False), ("pageNumbers", False),
])
def test_sensitive_fields(name, sensitive):
    assert is_sensitive_field(name) is sensitive


@pytest.mark.parametrize("header,expected", [
    ('form-data; name="attachment"; filename="a_merged_unsigned.pdf"', "a_merged_unsigned.pdf"),
    ('attachment; filename="x y.pdf"', "x y.pdf"),
    ("attachment; filename*=UTF-8''laporan%20keuangan.pdf", "laporan keuangan.pdf"),
    ("attachment; filename=plain.zip", "plain.zip"),
    (None, None),
    ("inline", None),
])
def test_content_disposition(header, expected):
    assert parse_content_disposition_filename(header) == expected
