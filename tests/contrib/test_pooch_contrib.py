# WriterAgent - tests for vendored Pooch subset
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from plugin.contrib.pooch.hashes import file_hash, hash_matches
from plugin.contrib.pooch.processors import Untar, Unzip


def test_hash_matches_sha256(tmp_path: Path) -> None:
    payload = b"harper-archive"
    path = tmp_path / "asset.bin"
    path.write_bytes(payload)
    digest = file_hash(str(path))
    assert hash_matches(str(path), f"sha256:{digest}")
    with pytest.raises(ValueError, match="does not match"):
        hash_matches(str(path), "sha256:deadbeef", strict=True)


def test_unzip_rejects_unsafe_member(tmp_path: Path) -> None:
    import zipfile

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../evil", b"nope")
    processor = Unzip()
    with pytest.raises(RuntimeError, match="Unsafe zip member"):
        processor(str(archive), "download", None)


def test_untar_rejects_unsafe_member(tmp_path: Path) -> None:
    import tarfile

    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo(name="../../evil")
        info.size = 4
        tar.addfile(info, BytesIO(b"nope"))
    processor = Untar()
    with pytest.raises(RuntimeError, match="Unsafe tar member"):
        processor(str(archive), "download", None)
