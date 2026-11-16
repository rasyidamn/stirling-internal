"""Path normalisation, which requests are allowed / audited / blocked, and
endpoint -> friendly operation name."""
from __future__ import annotations

import re

# Explicit friendly names for the most common Stirling endpoints (v3.3.0).
_OPERATIONS = {
    "/api/v1/general/merge-pdfs": "merge",
    "/api/v1/general/split-pages": "split",
    "/api/v1/general/split-pdf-by-sections": "split",
    "/api/v1/general/split-by-size-or-count": "split",
    "/api/v1/general/split-pdf-by-chapters": "split",
    "/api/v1/general/split-for-poster-print": "split",
    "/api/v1/misc/auto-split-pdf": "split",
    "/api/v1/misc/compress-pdf": "compress",
    "/api/v1/general/rotate-pdf": "rotate",
    "/api/v1/general/rearrange-pages": "rearrange-pages",
    "/api/v1/general/remove-pages": "remove-pages",
    "/api/v1/security/add-password": "add-password",
    "/api/v1/security/remove-password": "remove-password",
    "/api/v1/security/add-watermark": "watermark",
    "/api/v1/misc/ocr-pdf": "ocr",
}

# Small, UI-internal calls that change nothing worth auditing (investigasi.md §4).
AUDIT_EXEMPT_PREFIXES = (
    "/api/v1/automation/meter",
)

BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TRUTHY = frozenset({"true", "1", "yes", "on", "y", "t"})
_SENSITIVE_RE = re.compile(r"pass(word)?|passwd|pwd|secret|token|api[-_]?key|private[-_]?key", re.I)


def normalize_path(path: str) -> str | None:
    """Canonical form of an (already percent-decoded) request path, or None if
    it must be rejected. The proxy checks AND forwards this form, so Stirling
    never sees a path that differs from the one the rules were applied to.

    - `;matrix` parameters are dropped from every segment (Spring ignores them,
      so `/api/v1/admin;x/...` would otherwise slip past prefix checks);
    - empty segments (`//`) are collapsed;
    - `.` / `..` segments, a left-over `%` (double encoding) and backslashes
      are rejected outright.
    """
    if not path.startswith("/") or "\\" in path or "%" in path or "\x00" in path:
        return None
    segments = []
    for seg in path.split("/"):
        seg = seg.split(";", 1)[0]
        if not seg:
            continue
        if seg in (".", ".."):
            return None
        segments.append(seg)
    out = "/" + "/".join(segments)
    if path.endswith("/") and segments:
        out += "/"
    return out


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def operation_for(path: str) -> str:
    if path in _OPERATIONS:
        return _OPERATIONS[path]
    if path.startswith("/api/v1/convert/"):
        parts = path.removeprefix("/api/v1/convert/").split("/")
        if len(parts) == 2:
            return f"convert:{parts[0]}-to-{parts[1]}"
        return "convert"
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return last.removesuffix("-pdf") or path


def is_blocked(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(_under(path, p) for p in prefixes)


def is_api_path(path: str) -> bool:
    return path.lower().startswith("/api/")


def is_allowed_api(path: str, prefixes: tuple[str, ...]) -> bool:
    """Allowlist for /api/**. Anything not explicitly listed (e.g. admin APIs a
    future Stirling release adds) is refused. Empty tuple = allowlist disabled.
    Matching is case-sensitive, like Spring, so `/API/...` is refused too."""
    if not prefixes or not is_api_path(path):
        return True
    return any(_under(path, p) for p in prefixes)


def is_truthy(value: object) -> bool:
    if isinstance(value, list):
        return any(is_truthy(v) for v in value)
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def is_audited(method: str, path: str, content_type: str | None = None) -> bool:
    """Audit every state-changing API call (multipart or not), except a few
    UI-internal endpoints. `content_type` is accepted for backwards
    compatibility; it no longer decides whether a call is audited."""
    if method not in BODY_METHODS or not path.startswith("/api/v1/"):
        return False
    return not any(_under(path, p) for p in AUDIT_EXEMPT_PREFIXES)


def is_multipart(content_type: str | None) -> bool:
    return (content_type or "").lower().startswith("multipart/form-data")


def is_sensitive_field(name: str) -> bool:
    return bool(_SENSITIVE_RE.search(name or ""))
