# WriterAgent — unit tests for otp_zip helper functions
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import struct
from unittest.mock import patch
import zipfile

from plugin.draw.otp_zip import (
    MAX_MEMBER_BYTES,
    _find_member,
    extract_member,
    norm_zip_name,
    read_member,
)


def test_norm_zip_name():
    assert norm_zip_name("Pictures\\Image.PNG") == "pictures/image.png"
    assert norm_zip_name("./Thumbnails/thumb.png") == "thumbnails/thumb.png"
    assert norm_zip_name("styles.xml") == "styles.xml"
    assert norm_zip_name("./a") == "a"
    assert norm_zip_name(".hidden") == ".hidden"
    assert norm_zip_name("../a") == "../a"


def test_find_member(tmp_path: Path):
    otp = tmp_path / "test.otp"
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("Thumbnails/thumbnail.png", b"thumb")
        zf.writestr("styles.xml", b"<styles/>")

    with zipfile.ZipFile(otp) as zf:
        assert _find_member(zf, "thumbnails/thumbnail.png") == "Thumbnails/thumbnail.png"
        assert _find_member(zf, ".\\Thumbnails\\thumbnail.png") == "Thumbnails/thumbnail.png"
        assert _find_member(zf, "styles.xml") == "styles.xml"
        assert _find_member(zf, "missing.xml") is None


def test_read_member(tmp_path: Path):
    otp = tmp_path / "test.otp"
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("small.txt", b"hello world")
        zf.writestr("big.bin", b"x" * (MAX_MEMBER_BYTES + 100))
        zf.writestr("exact.bin", b"x" * MAX_MEMBER_BYTES)
        zf.writestr("over_custom.bin", b"x" * 11)

    with zipfile.ZipFile(otp) as zf:
        assert read_member(zf, "small.txt") == b"hello world"
        assert read_member(zf, "missing.txt") is None
        assert read_member(zf, "big.bin") is None
        exact = read_member(zf, "exact.bin")
        assert exact is not None and len(exact) == MAX_MEMBER_BYTES
        assert read_member(zf, "over_custom.bin", limit=10) is None


def test_extract_member(tmp_path: Path):
    otp = tmp_path / "test.otp"
    dest = tmp_path / "extracted"
    dest.mkdir()
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("Pictures/logo.png", b"png_data")
        zf.writestr("..", b"sneaky")

    with zipfile.ZipFile(otp) as zf:
        path = extract_member(zf, "Pictures/logo.png", str(dest), prefix="0_")
        assert path is not None
        assert Path(path).name == "0_logo.png"
        assert Path(path).read_bytes() == b"png_data"

        # Traversal rejected
        assert extract_member(zf, "..", str(dest)) is None


def test_extract_member_capped_and_safe_paths(tmp_path: Path):
    otp = tmp_path / "test_capped.otp"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("overcap.bin", b"x" * (MAX_MEMBER_BYTES + 10))
        zf.writestr("../..", b"sneaky")
        zf.writestr("Pictures/c.png", b"normal_pic")

    with zipfile.ZipFile(otp) as zf:
        # Over-cap member returns None
        assert extract_member(zf, "overcap.bin", str(dest_dir)) is None
        # Traversal names do not extract or create files outside dest_dir
        assert extract_member(zf, "../..", str(dest_dir)) is None
        # Normal member extracts
        out = extract_member(zf, "Pictures/c.png", str(dest_dir), prefix="test_")
        assert out is not None
        assert Path(out).read_bytes() == b"normal_pic"
        assert Path(out).parent == dest_dir


def test_extract_member_uniquifies_duplicate_basenames(tmp_path: Path):
    otp = tmp_path / "dup.otp"
    dest = tmp_path / "dest"
    dest.mkdir()
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("Pictures/a/logo.png", b"logo_a")
        zf.writestr("Pictures/b/logo.png", b"logo_b")

    with zipfile.ZipFile(otp) as zf:
        p1 = extract_member(zf, "Pictures/a/logo.png", str(dest), prefix="0_")
        p2 = extract_member(zf, "Pictures/b/logo.png", str(dest), prefix="0_")
        assert p1 is not None and p2 is not None
        assert Path(p1).name == "0_logo.png"
        assert Path(p2).name == "0_logo_1.png"
        assert Path(p1).read_bytes() == b"logo_a"
        assert Path(p2).read_bytes() == b"logo_b"


def test_extract_member_write_failure_leaves_preexisting_file(tmp_path: Path):
    otp = tmp_path / "fail.otp"
    dest = tmp_path / "dest"
    dest.mkdir()
    preexisting = dest / "0_logo.png"
    preexisting.write_bytes(b"keep")

    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("Pictures/logo.png", b"new_data")

    with zipfile.ZipFile(otp) as zf:
        with patch("builtins.open", side_effect=OSError("disk full")):
            res = extract_member(zf, "Pictures/logo.png", str(dest), prefix="0_")
        assert res is None
        assert preexisting.read_bytes() == b"keep"
        assert not (dest / "0_logo_1.png").exists()


def test_read_member_encrypted_returns_none(tmp_path: Path):
    otp = tmp_path / "enc.otp"
    dest = tmp_path / "dest"
    dest.mkdir()
    name = "Pictures/logo.png"
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr(name, b"some content")

    raw = bytearray(otp.read_bytes())
    i = raw.rfind(b"PK\x01\x02")
    struct.pack_into("<H", raw, i + 8, struct.unpack_from("<H", raw, i + 8)[0] | 1)
    otp.write_bytes(raw)

    with zipfile.ZipFile(otp) as zf:
        assert read_member(zf, name) is None
        assert extract_member(zf, name, str(dest)) is None
        assert not any(dest.iterdir())


def test_read_member_unsupported_compression_returns_none(tmp_path: Path):
    otp = tmp_path / "unsupp.otp"
    name = "Pictures/logo.png"
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr(name, b"some content")

    raw = bytearray(otp.read_bytes())
    i = raw.rfind(b"PK\x01\x02")
    struct.pack_into("<H", raw, i + 10, 99)
    otp.write_bytes(raw)

    with zipfile.ZipFile(otp) as zf:
        assert read_member(zf, name) is None
