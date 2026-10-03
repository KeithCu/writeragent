# WriterAgent — unit tests for Impress .otp look tags (no headed LO)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from plugin.draw.design_look import _MAX_MEMBER_BYTES, decode_png_rgb, derive_otp_look


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _rgb_png(pixels: list[tuple[int, int, int]], width: int, height: int) -> bytes:
    raw = b""
    for y in range(height):
        raw += b"\x00"
        for x in range(width):
            raw += bytes(pixels[y * width + x])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _write_otp(
    tmp_path: Path,
    *,
    name: str = "Demo.otp",
    thumb: bytes | None = None,
    pictures: dict[str, bytes] | None = None,
    styles: bytes | None = None,
) -> str:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.presentation-template")
        if thumb is not None:
            zf.writestr("Thumbnails/thumbnail.png", thumb)
        if pictures:
            for filename, data in pictures.items():
                zf.writestr("Pictures/%s" % filename, data)
        if styles is not None:
            zf.writestr("styles.xml", styles)
    return str(path)


def test_decode_png_rgb_roundtrip():
    pix = [(10, 20, 200), (200, 30, 10), (5, 5, 5), (250, 250, 250)]
    decoded = decode_png_rgb(_rgb_png(pix, 2, 2))
    assert decoded == pix


def _gray_png(width: int, height: int, bit_depth: int, sample: int) -> bytes:
    """Grayscale, filter None, every pixel *sample* (0..2**bit_depth-1)."""
    maxv = (1 << bit_depth) - 1
    row_len = (width * bit_depth + 7) // 8
    row = bytearray(row_len)
    for i in range(width):
        bit_pos = i * bit_depth
        shift = 8 - bit_depth - (bit_pos % 8)
        row[bit_pos // 8] |= (sample & maxv) << shift
    raw = b"".join(b"\x00" + bytes(row) for _y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def _gray1_png(width: int, height: int) -> bytes:
    """1-bit grayscale, filter None, every pixel 0. Solid rows deflate to a few KB."""
    return _gray_png(width, height, 1, 0)


def test_decode_png_rgb_rejects_large_1bit_before_pixel_tuples():
    # Same builder at a tiny size must still decode, so a corrupt PNG is
    # not why the large image is rejected. These rows are sample 0 (black).
    assert decode_png_rgb(_gray1_png(8, 1)) == [(0, 0, 0)] * 8

    # 4096×4096 1-bit: ~2MB of scanlines (under the 4MB raw cap) and a few KB
    # on disk, but one RGB tuple per pixel is ~16.7M objects.
    png = _gray1_png(4096, 4096)
    assert len(png) < 32 * 1024
    import tracemalloc

    tracemalloc.start()
    try:
        decoded = decode_png_rgb(png)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert decoded is None
    assert peak < 1024 * 1024


def test_decode_png_rgb_rejects_deflate_bomb_before_expanding():
    # IHDR is 1x1, so the declared-size cap does not trip. The IDAT inflates
    # far past that. zlib.decompress would materialize the whole bomb first.
    bomb = b"\x00" * 200_000
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bomb, 9)) + _chunk(b"IEND", b"")
    import tracemalloc

    tracemalloc.start()
    try:
        decoded = decode_png_rgb(png)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert decoded is None
    assert peak < len(bomb) // 2


def _forge_uncompressed_size(blob: bytearray, declared: int) -> None:
    """Set local-header and central-directory uncompressed sizes to *declared*.

    ``ZipInfo.file_size`` is the central-directory field. The local file
    header has the same value; a crafted ``.otp`` lies in either place.
    """
    eocd = blob.rfind(b"PK\x05\x06")
    assert eocd >= 0
    cd_off = struct.unpack_from("<I", blob, eocd + 16)[0]
    pos = cd_off
    while pos + 46 <= len(blob) and blob[pos : pos + 4] == b"PK\x01\x02":
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", blob, pos + 28)
        local_off = struct.unpack_from("<I", blob, pos + 42)[0]
        struct.pack_into("<I", blob, pos + 24, declared)
        assert blob[local_off : local_off + 4] == b"PK\x03\x04"
        struct.pack_into("<I", blob, local_off + 22, declared)
        pos += 46 + name_len + extra_len + comment_len


def test_forged_zip_file_size_cannot_inflate_past_member_cap(tmp_path):
    # 4× the member cap of zeros deflates to a few KB. Declared uncompressed
    # size is 64, under the cap, so the old file_size check called ZipFile.read
    # and zlib inflated the whole stream (ZipExtFile.MAX_N) before slicing.
    # Returning "" is not the proof: a CRC failure already did that, after
    # the multi-megabyte buffer existed. Peak allocation is the proof.
    payload = b"\x00" * (_MAX_MEMBER_BYTES * 4)
    declared = 64
    for member in ("Thumbnails/thumbnail.png", "styles.xml", "Pictures/chrome.svg"):
        otp = tmp_path / (member.replace("/", "_") + ".otp")
        with zipfile.ZipFile(otp, "w") as zf:
            zf.writestr(member, payload, compress_type=zipfile.ZIP_DEFLATED)
        blob = bytearray(otp.read_bytes())
        _forge_uncompressed_size(blob, declared)
        otp.write_bytes(blob)
        assert otp.stat().st_size < 64 * 1024
        with zipfile.ZipFile(otp) as zf:
            assert zf.getinfo(member).file_size == declared

        import tracemalloc

        tracemalloc.start()
        try:
            look = derive_otp_look(str(otp))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert look == ""
        assert peak < _MAX_MEMBER_BYTES


def test_low_bit_grayscale_white_thumbnail_is_light(tmp_path):
    for bit_depth in (1, 2, 4):
        white = (1 << bit_depth) - 1
        png = _gray_png(8, 2, bit_depth, white)
        assert decode_png_rgb(png) == [(255, 255, 255)] * 16
        path = _write_otp(tmp_path, name="White%d.otp" % bit_depth, thumb=png)
        assert derive_otp_look(path) == "light background"
    # Mid samples scale by 255/maxv, not the raw 0..maxv code.
    assert decode_png_rgb(_gray_png(4, 1, 2, 1)) == [(85, 85, 85)] * 4
    assert decode_png_rgb(_gray_png(2, 1, 4, 1)) == [(17, 17, 17)] * 2
    # 8-bit grayscale is already 0..255.
    assert decode_png_rgb(_gray_png(2, 1, 8, 200)) == [(200, 200, 200)] * 2


def test_deflated_light_thumbnail_still_reads(tmp_path):
    pix = [(240, 240, 240)] * (4 * 4)
    path = tmp_path / "Deflated.otp"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Thumbnails/thumbnail.png", _rgb_png(pix, 4, 4))
    assert derive_otp_look(str(path)) == "light background"


def test_dark_blue_thumb_and_single_svg_is_graphic_chrome(tmp_path):
    # Solid navy — Metropolis-like: dark + blue + one decorative graphic.
    pix = [(20, 40, 120)] * (8 * 8)
    path = _write_otp(
        tmp_path,
        thumb=_rgb_png(pix, 8, 8),
        pictures={"chrome.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>'},
    )
    look = derive_otp_look(path)
    assert look == "dark background; blue accents; graphic chrome"


def test_several_pictures_appends_illustrated(tmp_path):
    pix = [(250, 240, 230)] * (4 * 4)
    path = _write_otp(
        tmp_path,
        thumb=_rgb_png(pix, 4, 4),
        pictures={"a.png": b"x", "b.jpg": b"y"},
    )
    look = derive_otp_look(path)
    assert look == "light background; illustrated"


def test_styles_xml_fallback_when_no_thumbnail(tmp_path):
    path = _write_otp(
        tmp_path,
        styles=b'<style draw:fill-color="#0a1a3a" fo:color="#3a7bd5"/>',
    )
    look = derive_otp_look(path)
    assert "dark background" in look
    assert "blue accents" in look


def test_empty_look_when_zip_has_no_usable_signal(tmp_path):
    path = _write_otp(tmp_path)
    assert derive_otp_look(path) == ""


def test_corrupt_otp_does_not_raise(tmp_path):
    path = tmp_path / "Broken.otp"
    path.write_bytes(b"PK not a zip")
    assert derive_otp_look(str(path)) == ""


def _shipped_metropolis() -> Path | None:
    # Tests may search well-known install dirs; plugin code must not.
    for candidate in (
        Path("/usr/lib/libreoffice/share/template/common/presnt/Metropolis.otp"),
        Path("/usr/share/libreoffice/share/template/common/presnt/Metropolis.otp"),
        Path("/usr/lib64/libreoffice/share/template/common/presnt/Metropolis.otp"),
    ):
        if candidate.is_file():
            return candidate
    return None


def test_metropolis_look_from_shipped_template_if_present():
    otp = _shipped_metropolis()
    if otp is None:
        pytest.skip("no shipped Metropolis.otp on this machine")
    look = derive_otp_look(str(otp))
    assert "dark background" in look
    assert "blue" in look
    assert "graphic chrome" in look
