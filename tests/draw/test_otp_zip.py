# WriterAgent — unit tests for otp_zip helper functions
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import zipfile

from plugin.draw.otp_zip import (
    MAX_MEMBER_BYTES,
    extract_member,
    find_member,
    norm_zip_name,
    read_member,
)


def test_norm_zip_name():
    assert norm_zip_name("Pictures\\Image.PNG") == "pictures/image.png"
    assert norm_zip_name("./Thumbnails/thumb.png") == "thumbnails/thumb.png"
    assert norm_zip_name("styles.xml") == "styles.xml"


def test_find_member(tmp_path: Path):
    otp = tmp_path / "test.otp"
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("Thumbnails/thumbnail.png", b"thumb")
        zf.writestr("styles.xml", b"<styles/>")

    with zipfile.ZipFile(otp) as zf:
        assert find_member(zf, "thumbnails/thumbnail.png") == "Thumbnails/thumbnail.png"
        assert find_member(zf, ".\\Thumbnails\\thumbnail.png") == "Thumbnails/thumbnail.png"
        assert find_member(zf, "styles.xml") == "styles.xml"
        assert find_member(zf, "missing.xml") is None


def test_read_member(tmp_path: Path):
    otp = tmp_path / "test.otp"
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("small.txt", b"hello world")
        zf.writestr("big.bin", b"x" * (MAX_MEMBER_BYTES + 100))

    with zipfile.ZipFile(otp) as zf:
        assert read_member(zf, "small.txt") == b"hello world"
        assert read_member(zf, "missing.txt") is None
        assert read_member(zf, "big.bin") is None


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
