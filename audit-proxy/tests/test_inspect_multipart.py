import io

import httpx

from app.inspect_multipart import inspect_multipart
from tests.conftest import sha


def encode(files, data) -> tuple[bytes, str]:
    req = httpx.Request("POST", "http://x/", files=files, data=data)
    body = req.read()
    return body, req.headers["content-type"]


def test_files_params_hash_and_pages(pdf2, pdf3):
    body, ct = encode(
        [("fileInput", ("a.pdf", pdf2, "application/pdf")),
         ("fileInput", ("b.pdf", pdf3, "application/pdf"))],
        {"sortType": "orderProvided", "removeCertSign": "false"})
    res = inspect_multipart(io.BytesIO(body), ct)
    assert res.error is None
    assert res.params == {"sortType": "orderProvided", "removeCertSign": "false"}
    assert [i["filename"] for i in res.inputs] == ["a.pdf", "b.pdf"]
    assert res.inputs[0] == {
        "field": "fileInput", "filename": "a.pdf", "content_type": "application/pdf",
        "size_bytes": len(pdf2), "sha256": sha(pdf2), "pages": 2}
    assert res.inputs[1]["pages"] == 3
    assert res.inputs[1]["sha256"] == sha(pdf3)


def test_password_fields_are_redacted(pdf2):
    body, ct = encode([("fileInput", ("a.pdf", pdf2, "application/pdf"))],
                      {"password": "Rahasia123", "ownerPassword": "x", "keyLength": "256",
                       "permissions": ""})
    res = inspect_multipart(io.BytesIO(body), ct)
    assert res.params["password"] == "***"
    assert res.params["ownerPassword"] == "***"
    assert res.params["keyLength"] == "256"
    assert "Rahasia123" not in repr(res)


def test_empty_password_stays_empty(pdf2):
    body, ct = encode([("fileInput", ("a.pdf", pdf2, "application/pdf"))], {"password": ""})
    assert inspect_multipart(io.BytesIO(body), ct).params["password"] == ""


def test_non_pdf_file_has_null_pages():
    body, ct = encode([("fileInput", ("foto.jpg", b"\xff\xd8\xff\xe0 not a pdf", "image/jpeg"))], {})
    res = inspect_multipart(io.BytesIO(body), ct)
    assert res.inputs[0]["pages"] is None
    assert res.inputs[0]["size_bytes"] == len(b"\xff\xd8\xff\xe0 not a pdf")


def test_corrupt_pdf_has_null_pages():
    body, ct = encode([("fileInput", ("rusak.pdf", b"%PDF-1.7\ngarbage", "application/pdf"))], {})
    assert inspect_multipart(io.BytesIO(body), ct).inputs[0]["pages"] is None


def test_repeated_params_become_list(pdf2):
    body, ct = encode([("fileInput", ("a.pdf", pdf2, "application/pdf"))],
                      {"pageNumbers": ["1", "3"]})
    assert inspect_multipart(io.BytesIO(body), ct).params["pageNumbers"] == ["1", "3"]


def test_long_param_truncated(pdf2):
    body, ct = encode([("fileInput", ("a.pdf", pdf2, "application/pdf"))], {"clientFileIds": "x" * 5000})
    v = inspect_multipart(io.BytesIO(body), ct).params["clientFileIds"]
    assert len(v) == 1001 and v.endswith("…")


def test_utf8_filename(pdf2):
    body, ct = encode([("fileInput", ("laporan Q3 – final.pdf", pdf2, "application/pdf"))], {})
    assert inspect_multipart(io.BytesIO(body), ct).inputs[0]["filename"] == "laporan Q3 – final.pdf"


def test_missing_boundary():
    res = inspect_multipart(io.BytesIO(b"abc"), "multipart/form-data")
    assert res.error and res.inputs == []


def test_malformed_body_does_not_raise():
    res = inspect_multipart(io.BytesIO(b"--b\r\nbroken"), "multipart/form-data; boundary=b")
    # never raises; whatever could be parsed is returned
    assert isinstance(res.inputs, list)
