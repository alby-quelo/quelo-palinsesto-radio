# -*- coding: utf-8 -*-
"""Parse multipart/form-data (stdlib, senza cgi) per upload web."""

from __future__ import annotations


def _header_value(headers: bytes, name: str) -> str:
    low = name.lower().encode("ascii")
    for raw in headers.split(b"\r\n"):
        if b":" not in raw:
            continue
        key, val = raw.split(b":", 1)
        if key.strip().lower() == low:
            return val.strip().decode("latin-1", errors="replace")
    return ""


def _filename_from_disp(disp: str) -> str:
    # Content-Disposition: form-data; name="files"; filename="x.mp3"
    for part in disp.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            # filename*=UTF-8''encoded
            val = part.split("=", 1)[1].strip().strip('"')
            if "''" in val:
                val = val.split("''", 1)[1]
            from urllib.parse import unquote

            return unquote(val)
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return ""


def _field_name_from_disp(disp: str) -> str:
    for part in disp.split(";"):
        part = part.strip()
        if part.lower().startswith("name="):
            return part.split("=", 1)[1].strip().strip('"')
    return ""


def parse_multipart(
    body: bytes, content_type: str
) -> tuple[dict[str, str], list[tuple[str, str, bytes]]]:
    """Ritorna (campi_testo, lista (field_name, filename, content))."""
    ctype = content_type or ""
    if "boundary=" not in ctype:
        raise ValueError("multipart senza boundary")
    boundary = ctype.split("boundary=", 1)[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    if not boundary:
        raise ValueError("boundary vuoto")
    delim = b"--" + boundary.encode("ascii", errors="ignore")
    if not body:
        return {}, []
    parts = body.split(delim)
    fields: dict[str, str] = {}
    files: list[tuple[str, str, bytes]] = []
    for part in parts:
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"--"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if b"\r\n\r\n" not in part:
            continue
        header_blob, data = part.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disp = _header_value(header_blob, "Content-Disposition")
        if not disp:
            continue
        name = _field_name_from_disp(disp)
        filename = _filename_from_disp(disp)
        if filename:
            files.append((name or "files", filename, data))
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files
