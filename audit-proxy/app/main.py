"""audit-proxy: transparent reverse proxy in front of Stirling-PDF.

Every allowed request is forwarded (with a normalised path). State-changing
API calls (POST/PUT/PATCH/DELETE /api/v1/**) additionally produce one JSON line
in the audit log with who / what / when / input+output SHA-256 / result.
Requests refused by the proxy itself are audited too.
"""
from __future__ import annotations

import hashlib
import re
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, BinaryIO
from urllib.parse import parse_qsl, unquote

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.audit_log import AuditLogger
from app.config import Settings
from app.inspect_multipart import add_param, inspect_multipart
from app.operations import (
    is_allowed_api,
    is_api_path,
    is_audited,
    is_blocked,
    is_multipart,
    is_truthy,
    normalize_path,
    operation_for,
)

HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "accept-encoding",
}
CHUNK = 256 * 1024
SMALL_BODY_LIMIT = 64 * 1024  # buffer error bodies / urlencoded params up to this size
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


# --------------------------------------------------------------------------- helpers
def parse_content_disposition_filename(value: str | None) -> str | None:
    """Stirling sends e.g. `form-data; name="attachment"; filename="x.pdf"`."""
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.I)
    if m:
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8")
        except LookupError:
            return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', value, re.I)
    if m:
        return m.group(1).replace('\\"', '"')
    m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    return m.group(1).strip() if m else None


def client_ip(request: Request) -> str | None:
    # Caddy sets X-Forwarded-For; the right-most entry is the one added by the
    # nearest (trusted) proxy, so a client-supplied value cannot spoof it.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else None


def forward_headers(request: Request) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
    # Ask Stirling for identity encoding so we hash the real file bytes.
    out.append(("accept-encoding", "identity"))
    return out


def response_headers(upstream: httpx.Response, upstream_url: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for k, v in upstream.headers.multi_items():
        lk = k.lower()
        if lk in HOP_BY_HOP:
            continue
        if lk == "location" and v.startswith(upstream_url):
            v = v[len(upstream_url):] or "/"
        if lk == "set-cookie":
            continue  # handled below to keep multiple cookies
        headers[k] = v
    return headers


def build_response(upstream: httpx.Response, body: AsyncIterator[bytes],
                   upstream_url: str) -> Response:
    headers = response_headers(upstream, upstream_url)
    resp = StreamingResponse(body, status_code=upstream.status_code, headers=headers)
    for cookie in upstream.headers.get_list("set-cookie"):
        resp.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return resp


def _join(prev: str | None, msg: str) -> str:
    return f"{prev}; {msg}" if prev else msg


# --------------------------------------------------------------------------- app
def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None,
               audit_logger: AuditLogger | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logger = audit_logger or AuditLogger(
        settings.log_dir, settings.log_file, settings.log_retention_days, settings.timezone,
        settings.syslog_host, settings.syslog_port, settings.syslog_protocol)
    allowed_groups = {g.lower() for g in settings.allowed_groups}
    too_big_msg = f"File terlalu besar. Batas upload {settings.max_upload_mb} MB."

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(
            base_url=settings.upstream_url, transport=transport, follow_redirects=False,
            timeout=httpx.Timeout(settings.upstream_timeout_s, connect=10.0))
        n = settings.max_concurrent_uploads
        app.state.upload_sem = anyio.Semaphore(n) if n > 0 else None
        try:
            yield
        finally:
            await app.state.client.aclose()
            logger.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.audit = logger

    # ------------------------------------------------------------ audit record
    def base_record(request: Request, path: str) -> dict:
        h = request.headers
        groups = [g.strip() for g in (h.get("remote-groups") or "").split(",") if g.strip()]
        query = request.url.query
        return {
            "ts": logger.now().isoformat(timespec="milliseconds"),
            "event_id": str(uuid.uuid4()),
            "user": h.get("remote-user") or None,
            "name": h.get("remote-name") or None,
            "groups": groups,
            "email": h.get("remote-email") or None,
            "ip": client_ip(request),
            "user_agent": h.get("user-agent"),
            "method": request.method,
            "endpoint": f"{request.method} {path}",
            "query": query or None,
            "operation": operation_for(path),
            "params": {},
            "inputs": [],
            "status": None,
            "result": None,
            "output": None,
            "duration_ms": None,
            "error": None,
        }

    async def write_audit(rec: dict, started: float) -> None:
        rec["duration_ms"] = int((time.monotonic() - started) * 1000)
        try:
            await run_in_threadpool(logger.write, rec)
        except Exception as exc:  # noqa: BLE001 - logging must never break a request
            print(f"audit-proxy: unexpected audit failure: {exc}", file=sys.stderr)

    async def fail(rec: dict, started: float, status: int, result: str, error: str,
                   user_message: str | None = None) -> Response:
        """Finish `rec` as rejected/error, write it, return a short JSON error."""
        rec.update(status=status, result=result, error=_join(rec.get("error"), error))
        await write_audit(rec, started)
        return JSONResponse({"error": user_message or error}, status_code=status)

    async def reject(request: Request, path: str, status: int, message: str, started: float,
                     audit: bool = True) -> Response:
        if audit:
            return await fail(base_record(request, path), started, status, "rejected", message)
        return JSONResponse({"error": message}, status_code=status)

    # ------------------------------------------------------------ routes
    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        st: dict = {"status": "ok" if logger.ok else "degraded", "log_ok": logger.ok}
        # Requests through Caddy always carry X-Forwarded-For and only get this
        # summary. Details (paths, error text) are for in-container checks only.
        if "x-forwarded-for" not in request.headers:
            st.update(logger.status())
        return JSONResponse(st, status_code=200 if logger.ok else 503)

    @app.api_route("/{full_path:path}", methods=METHODS, include_in_schema=False)
    async def proxy(request: Request, full_path: str) -> Response:
        started = time.monotonic()
        raw_path = "/" + full_path
        audited_path = is_api_path(raw_path)

        # 1. identity: Caddy + Authelia must have set Remote-User.
        if not request.headers.get("remote-user"):
            print(f"audit-proxy: rejected request without Remote-User: {request.method} {raw_path} "
                  f"from {client_ip(request)}", file=sys.stderr)
            return await reject(request, raw_path, 401, "missing Remote-User (not authenticated)",
                                started, audit=audited_path)

        # 2. optional group check (AD groups forwarded by Authelia).
        if allowed_groups:
            groups = {g.strip().lower() for g in (request.headers.get("remote-groups") or "").split(",")}
            if not groups & allowed_groups:
                return await reject(request, raw_path, 403, "user is not in an allowed group",
                                    started, audit=True)

        # 3. canonical path: what we check is exactly what Stirling receives.
        path = normalize_path(raw_path)
        if path is None:
            return await reject(request, raw_path, 400, "invalid request path", started, audit=True)

        # 4. blocked admin/developer APIs; /api/** must also be on the allowlist.
        if is_blocked(path, settings.blocked_prefixes) or \
                not is_allowed_api(path, settings.allowed_api_prefixes):
            return await reject(request, path, 403, "endpoint disabled by administrator",
                                started, audit=True)

        # 5. async jobs are disabled (a job result could be fetched by another user).
        if audited_path and is_truthy(request.query_params.getlist("async")):
            return await reject(request, path, 403, "async jobs are disabled", started, audit=True)

        content_type = request.headers.get("content-type")
        if is_audited(request.method, path, content_type):
            # 6. optional fail-closed: no operation without a working audit log.
            if settings.audit_fail_closed and not logger.ok:
                return await reject(request, path, 503, "audit log unavailable (fail-closed)",
                                    started, audit=True)
            return await audited_request(request, path, content_type, started)
        return await passthrough(request, path)

    # ------------------------------------------------------------ passthrough
    async def passthrough(request: Request, path: str) -> Response:
        client: httpx.AsyncClient = request.app.state.client
        has_body = request.method not in ("GET", "HEAD", "OPTIONS") or request.headers.get("content-length")
        req = client.build_request(
            request.method, path, params=request.url.query or None,
            headers=forward_headers(request),
            content=request.stream() if has_body else None)
        try:
            upstream = await client.send(req, stream=True)
        except httpx.TimeoutException:
            return JSONResponse({"error": "Stirling-PDF timeout"}, status_code=504)
        except httpx.HTTPError as exc:
            print(f"audit-proxy: upstream error {exc!r}", file=sys.stderr)
            return JSONResponse({"error": "Stirling-PDF unavailable"}, status_code=502)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes(CHUNK):
                    yield chunk
            finally:
                await upstream.aclose()

        return build_response(upstream, body(), settings.upstream_url)

    # ------------------------------------------------------------ audited
    async def audited_request(request: Request, path: str, content_type: str | None,
                              started: float) -> Response:
        rec = base_record(request, path)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes:
            return await fail(rec, started, 413, "rejected",
                              f"upload too large: {int(declared)} bytes > {settings.max_upload_mb} MB",
                              too_big_msg)

        # Limit how many uploads sit in tmpfs (RAM) at the same time.
        sem: anyio.Semaphore | None = request.app.state.upload_sem
        acquired = False
        if sem is not None:
            try:
                sem.acquire_nowait()
                acquired = True
            except anyio.WouldBlock:
                with anyio.move_on_after(settings.upload_queue_timeout_s):
                    await sem.acquire()
                    acquired = True
            if not acquired:
                return await fail(rec, started, 503, "rejected",
                                  "server busy: too many concurrent uploads",
                                  "Server sedang sibuk. Coba lagi beberapa saat.")
        try:
            spool = tempfile.TemporaryFile(dir=settings.tmp_dir)
            try:
                outcome = await spool_and_send(request, path, content_type, rec, spool, started)
            finally:
                spool.close()  # request fully sent once Stirling's headers are back
        except OSError as exc:  # e.g. tmpfs full (ENOSPC)
            return await fail(rec, started, 507, "error",
                              f"cannot spool upload: {type(exc).__name__}: {exc}",
                              "Ruang sementara server penuh. Coba lagi beberapa saat.")
        finally:
            if acquired:
                sem.release()
        if isinstance(outcome, Response):
            return outcome
        upstream = outcome

        rec["status"] = upstream.status_code
        ok = 200 <= upstream.status_code < 300
        resp_ct = upstream.headers.get("content-type", "")
        out_name = parse_content_disposition_filename(upstream.headers.get("content-disposition"))

        async def body() -> AsyncIterator[bytes]:
            sha = hashlib.sha256()
            size = 0
            small = bytearray()
            completed = False
            try:
                async for chunk in upstream.aiter_bytes(CHUNK):
                    sha.update(chunk)
                    size += len(chunk)
                    if len(small) < SMALL_BODY_LIMIT:
                        small += chunk[: SMALL_BODY_LIMIT - len(small)]
                    yield chunk
                completed = True
            finally:
                # Shield: the audit line must be written even if the client went away.
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    if ok:
                        rec["result"] = "success"
                        rec["output"] = {"filename": out_name, "content_type": resp_ct or None,
                                         "size_bytes": size, "sha256": sha.hexdigest()}
                    else:
                        rec["result"] = "error"
                        msg = bytes(small[:500]).decode("utf-8", "replace").strip()
                        rec["error"] = _join(rec.get("error"), f"Stirling HTTP {upstream.status_code}"
                                             + (f": {msg}" if msg else ""))
                    if not completed:
                        rec["error"] = _join(rec.get("error"),
                                             "client disconnected before response completed")
                    await write_audit(rec, started)

        return build_response(upstream, body(), settings.upstream_url)

    async def spool_and_send(request: Request, path: str, content_type: str | None, rec: dict,
                             spool: BinaryIO, started: float) -> Response | httpx.Response:
        """Spool the body to tmpfs, inspect it, forward it. Returns Stirling's
        (streaming) response, or a finished error Response that is already audited."""
        limit = settings.max_upload_bytes
        body_sha = hashlib.sha256()
        total = 0
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    return await fail(rec, started, 413, "rejected",
                                      f"upload too large: > {settings.max_upload_mb} MB", too_big_msg)
                body_sha.update(chunk)
                await run_in_threadpool(spool.write, chunk)
        except ClientDisconnect:
            rec.update(status=499, result="error", error="client disconnected during upload")
            await write_audit(rec, started)
            return Response(status_code=499)

        if is_multipart(content_type):
            info = await run_in_threadpool(
                inspect_multipart, spool, content_type or "", settings.tmp_dir, True,
                settings.page_count_max_bytes)
            rec["params"] = info.params
            rec["inputs"] = info.inputs
            if info.error:
                rec["error"] = info.error
        elif total:
            # Non-multipart call: record that a body was sent (never its content).
            rec["request_body"] = {"content_type": content_type, "size_bytes": total,
                                   "sha256": body_sha.hexdigest()}
            if "x-www-form-urlencoded" in (content_type or "").lower() and total <= SMALL_BODY_LIMIT:
                await run_in_threadpool(spool.seek, 0)
                raw = (await run_in_threadpool(spool.read)).decode("utf-8", "replace")
                for k, v in parse_qsl(raw, keep_blank_values=True):
                    add_param(rec["params"], k, v)

        if is_truthy(rec["params"].get("async")):
            return await fail(rec, started, 403, "rejected", "async jobs are disabled")

        client: httpx.AsyncClient = request.app.state.client

        async def spool_reader() -> AsyncIterator[bytes]:
            await run_in_threadpool(spool.seek, 0)
            while chunk := await run_in_threadpool(spool.read, CHUNK):
                yield chunk

        headers = forward_headers(request)
        headers.append(("content-length", str(total)))
        req = client.build_request(
            request.method, path, params=request.url.query or None, headers=headers,
            content=spool_reader())
        try:
            return await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            timeout = isinstance(exc, httpx.TimeoutException)
            return await fail(rec, started, 504 if timeout else 502, "error",
                              "Stirling-PDF timeout" if timeout
                              else f"Stirling-PDF unavailable: {type(exc).__name__}")

    return app


# Run with: uvicorn app.main:create_app --factory
