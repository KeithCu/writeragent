# WriterAgent - adapted from Pooch v1.8.2 HTTPDownloader; uses urllib instead of requests.
from __future__ import annotations

import urllib.error
import urllib.request
from typing import BinaryIO


class HTTPDownloader:
    """Stream a URL to a local file using stdlib urllib."""

    def __init__(
        self,
        *,
        chunk_size: int = 1024 * 1024,
        timeout: float = 120,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.headers = dict(headers or {})

    def __call__(self, url: str, output_file: str | BinaryIO, pooch=None, check_only: bool = False) -> bool | None:
        del pooch
        request = urllib.request.Request(url, headers=self.headers, method="HEAD" if check_only else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if check_only:
                    return response.status == 200
                is_path = isinstance(output_file, str)
                handle: BinaryIO
                if is_path:
                    handle = open(output_file, "w+b")  # noqa: SIM115
                else:
                    handle = output_file
                try:
                    total = 0
                    while True:
                        chunk = response.read(self.chunk_size)
                        if not chunk:
                            break
                        total += len(chunk)
                        if self.max_bytes is not None and total > self.max_bytes:
                            raise RuntimeError(f"Download exceeded {self.max_bytes} bytes")
                        handle.write(chunk)
                        handle.flush()
                finally:
                    if is_path:
                        handle.close()
        except urllib.error.HTTPError as exc:
            if check_only:
                return False
            raise RuntimeError(f"HTTP download failed: {exc}") from exc
        return None
