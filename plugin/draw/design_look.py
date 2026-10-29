# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Short appearance tags for Impress ``.otp`` designs (no UNO, no vision).

``list_designs`` used to return only ``{id, name, path, url}``. Mercury-class
models then pick Metropolis by name without knowing it is dark/tech. This
module reads the template ZIP at list time:

1. Prefer ``Thumbnails/thumbnail.png`` (every shipped LO template has one).
2. Mood from average luminance; accent hue names skip near-black/white noise.
3. ``Pictures/`` count: several images → ``illustrated``; exactly one
   graphic → ``graphic chrome``.
4. If the thumbnail is missing or undecodable, scrape ``#RRGGBB`` from
   ``styles.xml`` + ``Pictures/*.svg``, stopping once
   ``_MAX_XML_SAMPLES`` hits are kept (and after ``_MAX_XML_SVG_MEMBERS``
   SVGs). Empty ``look`` rather than inventing.

Stdlib PNG decode (not Pillow): LibreOffice's Python typically has no PIL.
Member bytes come from ``otp_zip``; declared uncompressed size is not a
bound. Pixel, inflate, and XML/SVG caps, 1/2/4-bit scaling, and the alpha
cutoff are the constants below. We never load master pages or open the
document via Desktop.
"""

from __future__ import annotations

import colorsys
from functools import lru_cache
import logging
import os
import re
import struct
import zipfile
import zlib
from typing import Iterable

from plugin.draw.otp_zip import (
    MAX_MEMBER_BYTES,
    norm_zip_name as _norm_zip_name,
    read_member as _read_member,
)

log = logging.getLogger("writeragent.draw.design_look")

_MAX_RAW_PNG = 4 * 1024 * 1024
_MAX_SAMPLED_PIXELS = 1024
# One tuple per pixel, not scanline bytes. 4096×4096 1-bit stays under
# _MAX_RAW_PNG (~2MB inflated, a few KB on disk) and would be ~16.7M
# tuples. Shipped Impress thumbs are at most 512×384; 1024×1024 leaves
# room for a larger preview without the tuple explosion.
_MAX_DECODED_PIXELS = 1024 * 1024
# No-thumbnail fallback. The per-member read cap still opens every
# Pictures/*.svg and keeps every #RRGGBB. Shipped templates have at most
# four such SVGs and under a thousand hex hits; past this budget, more
# input does not improve mood or accent bins. Listing stops there.
_MAX_XML_SVG_MEMBERS = 8
_MAX_XML_SAMPLES = _MAX_SAMPLED_PIXELS

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_HEX_RE = re.compile(rb"#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?")

_IMAGE_EXT = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".wmf", ".emf", ".tif", ".tiff")

# Hue wheel bins in degrees (colorsys h * 360). Red wraps past 330.
_HUE_BINS: tuple[tuple[float, str], ...] = ((15.0, "red"), (45.0, "orange"), (70.0, "yellow"), (160.0, "green"), (200.0, "teal"), (255.0, "blue"), (290.0, "purple"), (330.0, "pink"), (361.0, "red"))
_SAMPLES_PER_PIXEL = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

# Rec. 709 luminance. Threshold chosen so Metropolis (~90) and Piano (~103)
# stay dark while mid-tone blueprint thumbs (~126) stay light.
_DARK_LUM = 110.0
_SKIP_V_LO = 0.12
# Near-white is high value *and* low saturation. Do not drop high-V yellows
# (Beehive / Yellow_Idea) — those are real accents.
_SKIP_S = 0.18
# Second accent must be at least a quarter of the top hue's samples.
_SECOND_ACCENT_RATIO = 0.25
# Thumbnail stride samples ~1024 pixels; 7–9 saturated specks (Piano, Progress)
# are drop-shadow / UI chrome, not a palette. XML fallback may have only a
# handful of hex hits, so keep that floor at 2.
_MIN_THUMB_ACCENT = 15
_MIN_XML_ACCENT = 2
# PNG alpha (0-255) below this is transparent: a white page behind a drop
# shadow must not dilute the mood. Shared by gray+alpha, RGBA, and palette tRNS.
_MIN_ALPHA = 16


@lru_cache(maxsize=128)
def _derive_otp_look_cached(path: str, mtime_ns: int, size: int) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            pic_tag = _picture_tag(names)
            samples = _thumbnail_samples(zf)
            min_count = _MIN_THUMB_ACCENT
            if not samples:
                samples = _xml_color_samples(zf, names)
                min_count = _MIN_XML_ACCENT
            mood, accents = _mood_and_accents(samples, min_count)
            return _format_look(mood, accents, pic_tag)
    except Exception:
        log.debug("derive_otp_look failed for %s", path, exc_info=True)
        return ""


def derive_otp_look(path: str) -> str:
    """One-line vibe for *path* (``.otp`` ZIP). Empty when nothing reliable."""
    try:
        st = os.stat(path)
        return _derive_otp_look_cached(path, st.st_mtime_ns, st.st_size)
    except Exception:
        log.debug("derive_otp_look stat failed for %s", path, exc_info=True)
        return ""


def _format_look(mood: str, accents: list[str], pic_tag: str) -> str:
    parts: list[str] = []
    if mood:
        parts.append(mood)
    if accents:
        parts.append("%s accents" % "/".join(accents))
    if pic_tag:
        parts.append(pic_tag)
    return "; ".join(parts)


def _picture_tag(names: Iterable[str]) -> str:
    images = 0
    for name in names:
        norm = _norm_zip_name(name)
        if norm.startswith("pictures/") and norm.endswith(_IMAGE_EXT):
            images += 1
    if images >= 2:
        return "illustrated"
    if images == 1:
        # Single decorative SVG/PNG/etc. (Metropolis ships one chrome SVG).
        return "graphic chrome"
    return ""


def _thumbnail_samples(zf: zipfile.ZipFile) -> list[tuple[int, int, int]]:
    raw = _read_member(zf, "thumbnails/thumbnail.png")
    if not raw:
        return []
    return decode_png_rgb(raw) or []


def _xml_color_samples(zf: zipfile.ZipFile, names: list[str]) -> list[tuple[int, int, int]]:
    """Hex colors from ``styles.xml`` and at most ``_MAX_XML_SVG_MEMBERS`` SVGs.

    ``list_designs`` / ``apply_design`` run this on the main thread, including
    user-writable template dirs. A per-member byte cap does not limit how many
    SVGs are opened; stop once ``_MAX_XML_SAMPLES`` hits are kept.
    """
    samples: list[tuple[int, int, int]] = []
    svgs: list[str] = []
    for n in names:
        norm = _norm_zip_name(n)
        if norm.startswith("pictures/") and norm.endswith(".svg") and len(svgs) < _MAX_XML_SVG_MEMBERS:
            svgs.append(n)
    to_read: list[str] = ["styles.xml"]
    to_read.extend(svgs)
    for name in to_read:
        if len(samples) >= _MAX_XML_SAMPLES:
            break
        blob = _read_member(zf, name)
        if blob:
            samples.extend(_hex_colors(blob, _MAX_XML_SAMPLES - len(samples)))
    return samples


def _hex_colors(blob: bytes, limit: int) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for match in _HEX_RE.finditer(blob):
        hex6 = match.group(1)
        alpha = match.group(2)
        if alpha is not None and int(alpha, 16) < 0x20:
            continue
        n = int(hex6, 16)
        out.append((n >> 16, (n >> 8) & 0xFF, n & 0xFF))
        if len(out) >= limit:
            break
    return out


def _mood_and_accents(samples: list[tuple[int, int, int]], min_count: int = _MIN_THUMB_ACCENT) -> tuple[str, list[str]]:
    if not samples:
        return "", []
    lum = sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in samples) / len(samples)
    mood = "dark background" if lum < _DARK_LUM else "light background"
    counts: dict[str, int] = {}
    for r, g, b in samples:
        name = _accent_hue_name(r, g, b)
        if name:
            counts[name] = counts.get(name, 0) + 1
    ranked = [pair for pair in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if pair[1] >= min_count]
    accents = [ranked[0][0]] if ranked else []
    if len(ranked) > 1 and ranked[1][1] >= max(min_count, int(ranked[0][1] * _SECOND_ACCENT_RATIO)):
        accents.append(ranked[1][0])
    return mood, accents


def _accent_hue_name(r: int, g: int, b: int) -> str:
    # Skip near-black / near-white / gray so drop-shadow and canvas do not
    # invent a hue (shipped thumbs often sit on a white page preview).
    hue, sat, val = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if val < _SKIP_V_LO or sat < _SKIP_S:
        return ""
    deg = hue * 360.0
    for bound, name in _HUE_BINS:
        if deg <= bound:
            return name
    return ""


# ---------------------------------------------------------------------------
# Minimal PNG decoder (8-bit RGB/RGBA/gray + 1/2/4/8-bit palette, no Adam7)
# ---------------------------------------------------------------------------


def _inflate_capped(data: bytes, limit: int) -> bytes | None:
    """Inflate *data*, or None if it is truncated or would pass *limit* bytes.

    ``zlib.decompress`` would build the whole output first. A thumbnail
    whose declared size is small can still be a deflate bomb, and
    ``list_designs`` / ``apply_design`` would allocate it on the main
    thread. ``max_length`` stops at ``limit + 1`` bytes.
    """
    if limit < 0:
        return None
    try:
        dec = zlib.decompressobj()
        out = dec.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or not dec.eof:
        return None
    return out


def decode_png_rgb(data: bytes) -> list[tuple[int, int, int]] | None:
    """Return RGB pixels or ``None`` if the PNG is unsupported, corrupt, or too large."""
    if len(data) < 33 or data[:8] != _PNG_SIG:
        return None
    width = height = bit_depth = color_type = interlace = -1
    palette: list[tuple[int, int, int]] = []
    trans: bytes = b""
    idat = bytearray()
    offset = 8
    n = len(data)
    while offset + 12 <= n:
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        tag = data[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > n:
            return None
        chunk = data[start:end]
        offset = end + 4
        if tag == b"IHDR":
            if length < 13:
                return None
            width, height, bit_depth, color_type, unused_comp, unused_filt, interlace = struct.unpack(">IIBBBBB", chunk[:13])
            if unused_comp != 0 or unused_filt != 0:
                return None
        elif tag == b"PLTE":
            if length % 3:
                return None
            palette = [(chunk[i], chunk[i + 1], chunk[i + 2]) for i in range(0, length, 3)]
        elif tag == b"tRNS":
            trans = chunk
        elif tag == b"IDAT":
            idat.extend(chunk)
            if len(idat) > MAX_MEMBER_BYTES:
                return None
        elif tag == b"IEND":
            break
    if width < 1 or height < 1 or width > 4096 or height > 4096:
        return None
    if width * height > _MAX_DECODED_PIXELS:
        return None
    if interlace != 0:
        return None
    if color_type not in (0, 2, 3, 4, 6):
        return None
    if bit_depth not in (1, 2, 4, 8):
        return None
    if color_type in (2, 4, 6) and bit_depth != 8:
        return None
    if color_type == 3 and not palette:
        return None
    spp = _SAMPLES_PER_PIXEL[color_type]
    row_bytes = 1 + (width * bit_depth * spp + 7) // 8
    expected = height * row_bytes
    if expected > _MAX_RAW_PNG:
        return None
    raw = _inflate_capped(bytes(idat), expected)
    if raw is None:
        return None
    if len(raw) < expected:
        return None
    bpp = max(1, (bit_depth * spp + 7) // 8)
    total_pixels = width * height
    step = max(1, total_pixels // _MAX_SAMPLED_PIXELS)
    pixels: list[tuple[int, int, int]] = []
    prior = bytes(row_bytes - 1)
    pixel_idx = 0
    for y in range(height):
        base = y * row_bytes
        filt = raw[base]
        row = bytearray(raw[base + 1 : base + row_bytes])
        if not _recon_scanline(filt, row, prior, bpp):
            return None
        prior = bytes(row)
        unpacked = _unpack_row(row, width, bit_depth, color_type, palette, trans)
        if unpacked is None:
            return None
        if len(pixels) < _MAX_SAMPLED_PIXELS:
            for x, pix in enumerate(unpacked):
                if (pixel_idx + x) % step == 0:
                    pixels.append(pix)
                    if len(pixels) >= _MAX_SAMPLED_PIXELS:
                        break
        pixel_idx += len(unpacked)
    return pixels


def _recon_scanline(filt: int, row: bytearray, prior: bytes, bpp: int) -> bool:
    n = len(row)
    if filt == 0:
        return True
    if filt == 1:
        for i in range(bpp, n):
            row[i] = (row[i] + row[i - bpp]) & 255
        return True
    if filt == 2:
        for i in range(n):
            row[i] = (row[i] + prior[i]) & 255
        return True
    if filt == 3:
        for i in range(n):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + ((left + prior[i]) >> 1)) & 255
        return True
    if filt == 4:
        for i in range(n):
            left = row[i - bpp] if i >= bpp else 0
            up = prior[i]
            ul = prior[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + _paeth(left, up, ul)) & 255
        return True
    return False


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unpack_row(row: bytes | bytearray, width: int, bit_depth: int, color_type: int, palette: list[tuple[int, int, int]], trans: bytes) -> list[tuple[int, int, int]] | None:
    if color_type == 2:
        return [(row[i], row[i + 1], row[i + 2]) for i in range(0, width * 3, 3)]
    if color_type == 6:
        out: list[tuple[int, int, int]] = []
        for i in range(0, width * 4, 4):
            if row[i + 3] < _MIN_ALPHA:
                continue
            out.append((row[i], row[i + 1], row[i + 2]))
        return out
    if color_type == 4:
        out = []
        for i in range(width):
            if row[i * 2 + 1] < _MIN_ALPHA:
                continue
            g = row[i * 2]
            out.append((g, g, g))
        return out
    # Gray (0) and indexed (3): one sample per pixel. 1/2/4-bit is MSB first.
    samples: list[int]
    if bit_depth == 8:
        samples = list(row[:width])
    else:
        samples = []
        maxv = (1 << bit_depth) - 1
        per_byte = 8 // bit_depth
        for byte in row:
            for shift in range(per_byte - 1, -1, -1):
                samples.append((byte >> (shift * bit_depth)) & maxv)
                if len(samples) >= width:
                    break
            if len(samples) >= width:
                break
    if color_type == 0:
        # 1/2/4-bit samples are 0..maxv. Copying them as 0..255 leaves an
        # all-white 1-bit thumbnail at sample 1, so the mood comes out dark.
        # PNG scales with g * 255 // maxv. 8-bit maxv is 255, so those
        # samples are unchanged.
        scale = (1 << bit_depth) - 1
        return [(g * 255 // scale, g * 255 // scale, g * 255 // scale) for g in samples]
    out = []
    pal_len = len(palette)
    for idx in samples:
        if idx >= pal_len:
            return None
        if idx < len(trans) and trans[idx] < _MIN_ALPHA:
            continue
        out.append(palette[idx])
    return out
