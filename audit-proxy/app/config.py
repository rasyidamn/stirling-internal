"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BLOCKED_PREFIXES = (
    # Admin / account / storage / AI / automation APIs that office staff never need.
    "/api/v1/admin",
    "/api/v1/database",
    "/api/v1/storage",
    "/api/v1/ai",
    "/api/v1/integration",
    "/api/v1/integrations",
    "/api/v1/workflow",
    "/api/v1/pipeline",
    "/api/v1/docparse",
    "/api/v1/user",
    "/api/v1/team",
    "/api/v1/invite",
    "/api/v1/audit",
    "/api/v1/usage",
    "/api/v1/ownership",
    "/api/v1/processing-folders",
    # Async jobs: the UI never uses them, and a job result could be fetched by
    # anyone who knows its id. Submitting with async=true is refused as well.
    "/api/v1/general/job",
    # API documentation
    "/swagger-ui",
    "/v1/api-docs",
    "/v3/api-docs",
)


# Allowlist for /api/**: only these prefixes reach Stirling (investigasi.md §3-4).
# Tool groups + the calls the UI itself makes. Everything else -> 403 (audited),
# so an admin API added by a future Stirling release is closed by default.
DEFAULT_ALLOWED_API_PREFIXES = (
    "/api/v1/general",
    "/api/v1/misc",
    "/api/v1/security",
    "/api/v1/convert",
    "/api/v1/filter",
    "/api/v1/analysis",
    "/api/v1/config",
    "/api/v1/info",
    "/api/v1/notifications",
    "/api/v1/policies",
    "/api/v1/automation/meter",
    "/api/v1/proprietary/ui-data",
)


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in ("1", "true", "yes", "on") if raw else default


@dataclass(frozen=True)
class Settings:
    upstream_url: str = "http://stirling-pdf:8080"
    log_dir: str = "/logs"
    log_file: str = "audit.jsonl"
    log_retention_days: int = 365
    timezone: str = "Asia/Jakarta"
    max_upload_mb: int = 200
    upstream_timeout_s: int = 300
    # Defence in depth: if set, Remote-Groups must contain at least one of these
    # (case-insensitive). Authelia already enforces access; this is a second check.
    allowed_groups: tuple[str, ...] = ()
    blocked_prefixes: tuple[str, ...] = DEFAULT_BLOCKED_PREFIXES
    # Empty tuple disables the allowlist (ALLOWED_API_PREFIXES=*).
    allowed_api_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_API_PREFIXES
    # Audited uploads processed at the same time (each spools up to
    # max_upload_mb in tmpfs). 0 = unlimited. Extra requests wait up to
    # upload_queue_timeout_s, then get 503.
    max_concurrent_uploads: int = 3
    upload_queue_timeout_s: int = 30
    # Page counting needs a second copy of each file; skip it above this size.
    page_count_max_mb: int = 50
    # true: refuse new operations (503) while the audit log cannot be written.
    audit_fail_closed: bool = False
    syslog_host: str = ""
    syslog_port: int = 514
    syslog_protocol: str = "udp"
    tmp_dir: str | None = None

    @property
    def page_count_max_bytes(self) -> int:
        return self.page_count_max_mb * 1024 * 1024

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        blocked = _csv(env("BLOCKED_PATH_PREFIXES"))
        allowed_raw = env("ALLOWED_API_PREFIXES", "").strip()
        if allowed_raw == "*":
            allowed: tuple[str, ...] = ()
        else:
            allowed = _csv(allowed_raw) or DEFAULT_ALLOWED_API_PREFIXES
        return cls(
            upstream_url=env("STIRLING_URL", cls.upstream_url).rstrip("/"),
            log_dir=env("LOG_DIR", cls.log_dir),
            log_retention_days=_int("LOG_RETENTION_DAYS", cls.log_retention_days),
            timezone=env("TZ_AUDIT", cls.timezone),
            max_upload_mb=_int("MAX_UPLOAD_MB", cls.max_upload_mb),
            upstream_timeout_s=_int("STIRLING_TIMEOUT", cls.upstream_timeout_s),
            allowed_groups=_csv(env("ALLOWED_GROUPS")),
            blocked_prefixes=blocked or DEFAULT_BLOCKED_PREFIXES,
            allowed_api_prefixes=allowed,
            max_concurrent_uploads=_int("MAX_CONCURRENT_UPLOADS", cls.max_concurrent_uploads),
            upload_queue_timeout_s=_int("UPLOAD_QUEUE_TIMEOUT", cls.upload_queue_timeout_s),
            page_count_max_mb=_int("PAGE_COUNT_MAX_MB", cls.page_count_max_mb),
            audit_fail_closed=_bool("AUDIT_FAIL_CLOSED", cls.audit_fail_closed),
            syslog_host=env("SYSLOG_HOST", "").strip(),
            syslog_port=_int("SYSLOG_PORT", 514),
            syslog_protocol=env("SYSLOG_PROTOCOL", "udp").strip().lower(),
            tmp_dir=env("TMP_DIR") or None,
        )
