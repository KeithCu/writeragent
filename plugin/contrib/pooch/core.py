# WriterAgent - adapted from Pooch v1.8.2 pooch/core.py (BSD-3-Clause), pruned to retrieve().
from __future__ import annotations

import os
import shutil
import time
import urllib.error
from pathlib import Path

from plugin.contrib.pooch.downloaders import HTTPDownloader
from plugin.contrib.pooch.hashes import file_hash, hash_matches
from plugin.contrib.pooch.utils import get_logger, make_local_storage, temporary_file


def download_action(path: Path, known_hash: str | None) -> tuple[str, str]:
    if not path.exists():
        return "download", "Downloading"
    if not hash_matches(str(path), known_hash):
        return "update", "Updating"
    return "fetch", "Fetching"


def stream_download(url: str, fname: Path, known_hash: str | None, downloader, *, pooch=None, retry_if_failed: int = 0) -> None:
    del pooch
    if not fname.parent.exists():
        os.makedirs(str(fname.parent), exist_ok=True)
    download_attempts = 1 + retry_if_failed
    max_wait = 10
    for attempt in range(download_attempts):
        try:
            with temporary_file(path=str(fname.parent)) as tmp:
                downloader(url, tmp, None)
                hash_matches(tmp, known_hash, strict=True, source=str(fname.name))
                shutil.move(tmp, str(fname))
            return
        except (ValueError, urllib.error.URLError, RuntimeError):
            if attempt == download_attempts - 1:
                raise
            retries_left = download_attempts - (attempt + 1)
            get_logger().info(
                "Failed to download %r. Will attempt the download again %d more time(s).",
                str(fname.name),
                retries_left,
            )
            time.sleep(min(attempt + 1, max_wait))


def retrieve(
    url: str,
    known_hash: str | None,
    *,
    fname: str | None = None,
    path: str | Path | None = None,
    processor=None,
    downloader=None,
    retry_if_failed: int = 2,
) -> str | list[str]:
    if path is None:
        raise ValueError("A path must be given to store the downloaded file.")
    if fname is None:
        fname = os.path.basename(str(url).split("?")[0])
    path = Path(path)
    full_path = path.resolve() / fname
    action, verb = download_action(full_path, known_hash)

    if action in ("download", "update"):
        make_local_storage(path)
        get_logger().info("%s data from %r to file %r.", verb, url, str(full_path))
        if downloader is None:
            downloader = HTTPDownloader()
        stream_download(url, full_path, known_hash, downloader, retry_if_failed=retry_if_failed)
        if known_hash is None:
            get_logger().info(
                "SHA256 hash of downloaded file: %s",
                file_hash(str(full_path)),
            )

    if processor is not None:
        return processor(str(full_path), action, None)
    return str(full_path)
