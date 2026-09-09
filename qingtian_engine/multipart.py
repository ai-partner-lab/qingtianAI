"""Bounded HTTP form-data decoding using Python's maintained email parser.

No CGI dependency, filesystem access, nested MIME, or transfer decoding. The
request cap preserves the intake's 100 MiB attachment budget plus form overhead.
"""
from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
import io
import re
from typing import BinaryIO


MAX_MULTIPART_BYTES = 102 * 1024 * 1024
MAX_PARTS = 32
MAX_PART_HEADER_BYTES = 8192
MAX_FIELD_BYTES = 128 * 1024


class MultipartError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class FormFile:
    name: str
    filename: str
    content_type: str
    stream: io.BytesIO


@dataclass
class MultipartForm:
    fields: dict[str, str]
    files: list[FormFile]

    def close(self) -> None:
        for item in self.files:
            item.stream.close()


def read_body(stream: BinaryIO, length: str, maximum: int) -> bytes:
    if not isinstance(length, str) or re.fullmatch(r"[0-9]+", length) is None:
        raise MultipartError("Content-Length must be a nonnegative integer")
    size = int(length)
    if size > maximum:
        raise MultipartError("Request body exceeds the configured limit", 413)
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(min(64 * 1024, size - len(chunks)))
        if not block:
            raise MultipartError("Truncated request body")
        chunks.extend(block)
    return bytes(chunks)


def parse_multipart(content_type: str, body: bytes) -> MultipartForm:
    if len(body) > MAX_MULTIPART_BYTES:
        raise MultipartError("Request body exceeds the configured limit", 413)
    if len(content_type) > 512 or any(char in content_type for char in "\r\n\x00"):
        raise MultipartError("Invalid multipart Content-Type")
    try:
        header = content_type.encode("ascii")
    except UnicodeEncodeError:
        raise MultipartError("Invalid multipart Content-Type") from None
    envelope = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + header + b"\r\n\r\n")
    boundary = envelope.get_boundary()
    if (envelope.get_content_type() != "multipart/form-data" or not boundary
            or re.fullmatch(r"[0-9A-Za-z'()+_,./:=? -]{1,70}", boundary) is None
            or boundary.endswith(" ")):
        raise MultipartError("A valid multipart/form-data boundary is required")
    delimiter = b"--" + boundary.encode("ascii")
    # Bound parser work before creating MIME objects and reject malformed tails.
    if body in (delimiter + b"--", delimiter + b"--\r\n"):
        return MultipartForm({}, [])
    if (not body.startswith(delimiter + b"\r\n")
            or not (body.endswith(delimiter + b"--\r\n") or body.endswith(delimiter + b"--"))):
        raise MultipartError("Malformed or incomplete multipart boundaries")
    segments = body.split(b"\r\n" + delimiter)
    if len(segments) - 1 > MAX_PARTS:
        raise MultipartError("Too many multipart fields or files", 413)
    for segment in segments[:-1]:
        part_head = segment.split(b"\r\n\r\n", 1)
        if len(part_head) != 2 or len(part_head[0]) > MAX_PART_HEADER_BYTES:
            raise MultipartError("Malformed or oversized multipart headers")
    message = BytesParser(policy=policy.default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + header + b"\r\n\r\n" + body
    )
    if message.defects or not message.is_multipart():
        raise MultipartError("Malformed multipart body")
    fields: dict[str, str] = {}
    uploads: list[FormFile] = []
    try:
        for part in message.iter_parts():
            if part.defects or part.is_multipart() or part.get_content_disposition() != "form-data":
                raise MultipartError("Nested or malformed multipart fields are not supported")
            if len(part.get_all("Content-Disposition", [])) != 1 or len(part.get_all("Content-Type", [])) > 1:
                raise MultipartError("Duplicate multipart headers")
            if part.get("Content-Transfer-Encoding", "binary").lower() not in {"binary", "8bit"}:
                raise MultipartError("Multipart transfer encoding is not supported")
            name = part.get_param("name", header="content-disposition")
            if not isinstance(name, str) or not name or len(name) > 128 or any(ord(c) < 32 for c in name):
                raise MultipartError("Invalid multipart field name")
            value = part.get_payload(decode=True)
            if not isinstance(value, bytes):
                raise MultipartError("Invalid multipart payload")
            filename = part.get_filename()
            if filename is not None:
                if not isinstance(filename, str) or len(filename) > 1024:
                    raise MultipartError("Invalid attachment filename")
                uploads.append(FormFile(name, filename, part.get_content_type(), io.BytesIO(value)))
            else:
                if name in fields:
                    raise MultipartError("Duplicate multipart field")
                if len(value) > MAX_FIELD_BYTES:
                    raise MultipartError("Multipart field is too large", 413)
                try:
                    fields[name] = value.decode("utf-8")
                except UnicodeDecodeError:
                    raise MultipartError("Multipart fields must be UTF-8") from None
        return MultipartForm(fields, uploads)
    except Exception:
        for item in uploads:
            item.stream.close()
        raise
