import hashlib
import io
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter


def make_pdf(pages: int) -> bytes:
    """Synthetic PDF with N blank pages (never real documents)."""
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_audit(log_dir: Path) -> list[dict]:
    f = log_dir / "audit.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def pdf2() -> bytes:
    return make_pdf(2)


@pytest.fixture
def pdf3() -> bytes:
    return make_pdf(3)
