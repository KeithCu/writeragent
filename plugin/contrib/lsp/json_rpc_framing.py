# WriterAgent - adapted from pylspclient json_rpc_endpoint.py (MIT).
from __future__ import annotations

import json
from typing import BinaryIO


_LSP_MAX_FRAME_BYTES = 10 * 1024 * 1024
_LEN_HEADER = "Content-Length: "
_TYPE_HEADER = "Content-Type: "


def read_exactly(stream: BinaryIO, nbytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = nbytes
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise RuntimeError("Unexpected EOF while reading LSP frame body")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def encode_frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def read_frame(stream: BinaryIO) -> dict | None:
    """Read one LSP Content-Length framed JSON-RPC message from *stream*."""
    message_size: int | None = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line_str = line.decode("utf-8")
        if not line_str.endswith("\r\n"):
            raise RuntimeError("Bad LSP header: missing CRLF")
        line_str = line_str[:-2]
        if line_str == "":
            break
        if line_str.startswith(_LEN_HEADER):
            raw = line_str[len(_LEN_HEADER) :].strip()
            if not raw.isdigit():
                raise RuntimeError(f"Bad LSP Content-Length header: {raw!r}")
            message_size = int(raw)
        elif line_str.startswith(_TYPE_HEADER):
            continue
        else:
            key, _, value = line_str.partition(":")
            if key.strip().lower() == "content-length":
                raw = value.strip()
                if not raw.isdigit():
                    raise RuntimeError(f"Bad LSP Content-Length header: {raw!r}")
                message_size = int(raw)

    if message_size is None:
        raise RuntimeError("LSP frame missing Content-Length header")
    if message_size <= 0 or message_size > _LSP_MAX_FRAME_BYTES:
        raise RuntimeError(f"Rejecting LSP frame with Content-Length={message_size}")

    body = read_exactly(stream, message_size).decode("utf-8")
    return json.loads(body)


def write_frame(stream: BinaryIO, payload: dict) -> None:
    stream.write(encode_frame(payload))
    stream.flush()
