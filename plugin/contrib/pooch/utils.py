# WriterAgent - vendored from Pooch v1.8.2 pooch/utils.py (BSD-3-Clause), pruned.
from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

LOGGER = logging.Logger("pooch")
LOGGER.addHandler(logging.StreamHandler())


def get_logger() -> logging.Logger:
    return LOGGER


def make_local_storage(path: str | Path, env: str | None = None) -> None:
    path = str(path)
    action = "create" if not os.path.exists(path) else "write to"
    try:
        if action == "create":
            os.makedirs(path, exist_ok=True)
        else:
            with tempfile.NamedTemporaryFile(dir=path):
                pass
    except PermissionError as error:
        message = [
            str(error),
            f"| Pooch could not {action} data cache folder {path!r}.",
            "Will not be able to download data files.",
        ]
        if env is not None:
            message.append(f"Use environment variable {env!r} to specify a different location.")
        raise PermissionError(" ".join(message)) from error


@contextmanager
def temporary_file(path: str | Path | None = None):
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=path)
    tmp.close()
    try:
        yield tmp.name
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
