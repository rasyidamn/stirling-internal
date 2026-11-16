"""Inspect a spooled multipart/form-data body: collect non-file params and
per-file metadata (name, size, sha256, page count). File contents only ever
live in anonymous temp files that are deleted as soon as they are closed."""
from __future__ import annotations

import hashlib
import logging
import tempfile
from dataclasses import dataclass, field
from typing import BinaryIO

from pypdf import PdfReader
from python_multipart.multipart import MultipartParser, parse_options_header

from app.operations import is_sensitive_field

log = logging.getLogger("audit-proxy")

MAX_PARAM_LEN = 1000
MAX_PARAMS = 100
CHUNK = 256 * 1024
REDACTED = "***"

logging.getLogger("pypdf").setLevel(logging.ERROR)


@dataclass
class _Part:
    headers: dict[str, bytes] = field(default_factory=dict)
    name: str = ""
    filename: str | None = None
    is_file: bool = False
    content_type: str | None = None
    size: int = 0
    sha: "hashlib._Hash" = field(default_factory=hashlib.sha256)
    text: bytearray = field(default_factory=bytearray)
    tmp: BinaryIO | None = None


@dataclass
class InspectResult:
    params: dict[str, object] = field(default_factory=dict)
    inputs: list[dict] = field(default_factory=list)
    error: str | None = None


def boundary_from_content_type(content_type: str) -> bytes | None:
    _, opts = parse_options_header(content_type)
    b = opts.get(b"boundary")
    return b or None


def count_pdf_pages(fp: BinaryIO) -> int | None:
    try:
        fp.seek(0)
        head = fp.read(1024)
        if b"%PDF" not in head:
            return None
        fp.seek(0)
        reader = PdfReader(fp, strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return None
        return len(reader.pages)
    except Exception:  # noqa: BLE001 - page count is best effort
        return None


def add_param(params: dict, name: str, value: str) -> None:
    if len(params) >= MAX_PARAMS and name not in params:
        return
    if is_sensitive_field(name):
        value = REDACTED if value else ""
    elif len(value) > MAX_PARAM_LEN:
        value = value[:MAX_PARAM_LEN] + "…"
    if name in params:
        prev = params[name]
        params[name] = (prev if isinstance(prev, list) else [prev]) + [value]
    else:
        params[name] = value


def inspect_multipart(body: BinaryIO, content_type: str, tmp_dir: str | None = None,
                      count_pages: bool = True,
                      page_count_max_bytes: int | None = None) -> InspectResult:
    """Parse the whole spooled body. Never raises: problems are reported in .error.

    Page counting keeps a temporary copy of each file part; parts larger than
    `page_count_max_bytes` are hashed but not counted (pages=None), so a big
    upload never needs a second full copy in tmpfs."""
    result = InspectResult()
    boundary = boundary_from_content_type(content_type)
    if not boundary:
        result.error = "multipart without boundary"
        return result

    state: dict = {"part": None, "hname": bytearray(), "hval": bytearray()}

    def on_part_begin() -> None:
        state["part"] = _Part()

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["hname"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["hval"] += data[start:end]

    def on_header_end() -> None:
        state["part"].headers[bytes(state["hname"]).lower().decode("latin-1")] = bytes(state["hval"])
        state["hname"].clear()
        state["hval"].clear()

    def on_headers_finished() -> None:
        part: _Part = state["part"]
        _, opts = parse_options_header(part.headers.get("content-disposition", b""))
        part.name = opts.get(b"name", b"").decode("utf-8", "replace")
        fn = opts.get(b"filename*") or opts.get(b"filename")
        if fn is not None:
            fn_s = fn.decode("utf-8", "replace")
            if fn_s.lower().startswith("utf-8''"):
                from urllib.parse import unquote
                fn_s = unquote(fn_s[7:])
            part.filename = fn_s
            part.is_file = True
            if count_pages:
                part.tmp = tempfile.TemporaryFile(dir=tmp_dir)
        ct = part.headers.get("content-type")
        part.content_type = ct.decode("latin-1") if ct else None

    def on_part_data(data: bytes, start: int, end: int) -> None:
        part: _Part = state["part"]
        chunk = data[start:end]
        if part.is_file:
            part.size += len(chunk)
            part.sha.update(chunk)
            if part.tmp is not None:
                if page_count_max_bytes is not None and part.size > page_count_max_bytes:
                    part.tmp.close()  # too big to count: drop the copy now
                    part.tmp = None
                else:
                    part.tmp.write(chunk)
        elif len(part.text) <= MAX_PARAM_LEN * 4:
            part.text += chunk

    def on_part_end() -> None:
        part: _Part = state["part"]
        if part.is_file:
            pages = None
            if part.tmp is not None:
                pages = count_pdf_pages(part.tmp) if part.size else None
                part.tmp.close()  # anonymous temp file -> deleted
            result.inputs.append({
                "field": part.name,
                "filename": part.filename,
                "content_type": part.content_type,
                "size_bytes": part.size,
                "sha256": part.sha.hexdigest(),
                "pages": pages,
            })
        else:
            add_param(result.params, part.name, part.text.decode("utf-8", "replace"))
        state["part"] = None

    parser = MultipartParser(boundary, {
        "on_part_begin": on_part_begin,
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
    }, max_header_count=32, max_header_size=16384)

    try:
        body.seek(0)
        while chunk := body.read(CHUNK):
            parser.write(chunk)
        parser.finalize()
    except Exception as exc:  # noqa: BLE001 - malformed body still gets forwarded
        result.error = f"multipart parse error: {type(exc).__name__}"
        part = state.get("part")
        if part is not None and part.tmp is not None:
            part.tmp.close()
    return result
