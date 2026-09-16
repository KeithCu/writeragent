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
   ``styles.xml`` + ``Pictures/*.svg``. Empty ``look`` rather than inventing.

Stdlib PNG decode (not Pillow): the extension runs in LibreOffice's Python,
which typically has no PIL. Thumbnails are tiny; we never load master pages
or open the document via Desktop.
"""

from __future__ import annotations

import colorsys
import logging
import re
import struct
import zipfile
import zlib
from typing import Iterable

log = logging.getLogger("writeragent.draw.design_look")

# Caps so a hostile/corrupt user ``.otp`` cannot inflate listing.
_MAX_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_RAW_PNG = 4 * 1024 * 1024
_MAX_SAMPLED_PIXELS = 1024

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_HEX_RE = re.compile(rb"#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?")

_IMAGE_EXT = (
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".wmf",
    ".emf",
    ".tif",
    ".tiff",
)

# Hue wheel bins in degrees (colorsys h * 360). Red wraps past 330.
_HUE_BINS: tuple[tuple[float, str], ...] = (
    (15.0, "red"),
    (45.0, "orange"),
    (70.0, "yellow"),
    (160.0, "green"),
    (200.0, "teal"),
    (255.0, "blue"),
    (290.0, "purple"),
    (330.0, "pink"),
    (361.0, "red"),
)

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


def derive_otp_look(path: str) -> str:
    """One-line vibe for *path* (``.otp`` ZIP). Empty when nothing reliable."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            pic_tag = _picture_tag(names)
            samples = _thumbnail_samples(zf, names)
            if not samples:
                samples = _xml_color_samples(zf, names)
            mood, accents = _mood_and_accents(samples)
            return _format_look(mood, accents, pic_tag)
    except Exception:
        log.debug("derive_otp_look failed for %s", path, exc_info=True)
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


def _norm_zip_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("./").lower()


def _picture_tag(names: Iterable[str]) -> str:
    pics = [
        n
        for n in names
        if _norm_zip_name(n).startswith("pictures/")
        and not _norm_zip_name(n).endswith("/")
        and _norm_zip_name(n).rsplit("/", 1)[-1]
    ]
    images = [n for n in pics if _norm_zip_name(n).endswith(_IMAGE_EXT)]
    if len(images) >= 2:
        return "illustrated"
    if len(images) == 1:
        # Single decorative SVG/PNG/etc. (Metropolis ships one chrome SVG).
        return "graphic chrome"
    return ""


def _read_member(zf: zipfile.ZipFile, name: str, limit: int = _MAX_MEMBER_BYTES) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    data = zf.read(name)
    if len(data) > limit:
        return None
    return data


def _thumbnail_samples(zf: zipfile.ZipFile, names: list[str]) -> list[tuple[int, int, int]]:
    thumb_name = ""
    for n in names:
        if _norm_zip_name(n) == "thumbnails/thumbnail.png":
            thumb_name = n
            break
    if not thumb_name:
        return []
    raw = _read_member(zf, thumb_name)
    if not raw:
        return []
    pixels = decode_png_rgb(raw)
    if not pixels:
        return []
    return _downsample(pixels)


def _xml_color_samples(zf: zipfile.ZipFile, names: list[str]) -> list[tuple[int, int, int]]:
    """Fallback when the thumbnail is missing: hex colors only, no invention."""
    samples: list[tuple[int, int, int]] = []
    for n in names:
        norm = _norm_zip_name(n)
        if norm == "styles.xml" or (norm.startswith("pictures/") and norm.endswith(".svg")):
            blob = _read_member(zf, n)
            if blob:
                samples.extend(_hex_colors(blob))
    return samples


def _hex_colors(blob: bytes) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for match in _HEX_RE.finditer(blob):
        hex6 = match.group(1)
        alpha = match.group(2)
        if alpha is not None and int(alpha, 16) < 0x20:
            continue
        r = int(hex6[0:2], 16)
        g = int(hex6[2:4], 16)
        b = int(hex6[4:6], 16)
        out.append((r, g, b))
    return out


def _downsample(pixels: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    n = len(pixels)
    if n <= _MAX_SAMPLED_PIXELS:
        return pixels
    step = max(1, n // _MAX_SAMPLED_PIXELS)
    return pixels[::step][:_MAX_SAMPLED_PIXELS]


def _mood_and_accents(samples: list[tuple[int, int, int]]) -> tuple[str, list[str]]:
    if not samples:
        return "", []
    lum = sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in samples) / len(samples)
    mood = "dark background" if lum < _DARK_LUM else "light background"
    counts: dict[str, int] = {}
    for r, g, b in samples:
        name = _accent_hue_name(r, g, b)
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return mood, []
    min_count = _MIN_XML_ACCENT if len(samples) < 50 else _MIN_THUMB_ACCENT
    ranked = [
        pair
        for pair in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if pair[1] >= min_count
    ]
    if not ranked:
        return mood, []
    accents = [ranked[0][0]]
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
    return "red"


# ---------------------------------------------------------------------------
# Minimal PNG decoder (8-bit RGB/RGBA/gray + 1/2/4/8-bit palette, no Adam7)
# ---------------------------------------------------------------------------


def decode_png_rgb(data: bytes) -> list[tuple[int, int, int]] | None:
    """Return RGB pixels or ``None`` if the PNG is unsupported/corrupt."""
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
            width, height, bit_depth, color_type, unused_comp, unused_filt, interlace = struct.unpack(
                ">IIBBBBB", chunk[:13]
            )
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
            if len(idat) > _MAX_MEMBER_BYTES:
                return None
        elif tag == b"IEND":
            break
    if width < 1 or height < 1 or width > 4096 or height > 4096:
        return None
    if interlace != 0:
        return None
    if color_type not in (0, 2, 3, 4, 6):
        return None
    if bit_depth not in (1, 2, 4, 8):
        return None
    if color_type == 3 and not palette:
        return None
    spp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    row_bytes = 1 + (width * bit_depth * spp + 7) // 8
    expected = height * row_bytes
    if expected > _MAX_RAW_PNG:
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    if len(raw) < expected:
        return None
    bpp = max(1, (bit_depth * spp + 7) // 8)
    pixels: list[tuple[int, int, int]] = []
    prior = bytes(row_bytes - 1)
    pos = 0
    rows_left = height
    while rows_left:
        rows_left -= 1
        filt = raw[pos]
        pos += 1
        row = bytearray(raw[pos : pos + row_bytes - 1])
        pos += row_bytes - 1
        if len(row) != row_bytes - 1:
            return None
        if not _recon_scanline(filt, row, prior, bpp):
            return None
        prior = bytes(row)
        unpacked = _unpack_row(row, width, bit_depth, color_type, palette, trans)
        if unpacked is None:
            return None
        pixels.extend(unpacked)
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


def _unpack_row(
    row: bytes,
    width: int,
    bit_depth: int,
    color_type: int,
    palette: list[tuple[int, int, int]],
    trans: bytes,
) -> list[tuple[int, int, int]] | None:
    samples = _unpack_samples(row, width, bit_depth, color_type)
    if samples is None:
        return None
    out: list[tuple[int, int, int]] = []
    if color_type == 0:
        for i in range(width):
            g = samples[i]
            out.append((g, g, g))
        return out
    if color_type == 4:
        for i in range(width):
            g = samples[i * 2]
            a = samples[i * 2 + 1]
            if a < 16:
                continue
            out.append((g, g, g))
        return out
    if color_type == 2:
        for i in range(width):
            j = i * 3
            out.append((samples[j], samples[j + 1], samples[j + 2]))
        return out
    if color_type == 6:
        for i in range(width):
            j = i * 4
            a = samples[j + 3]
            if a < 16:
                continue
            out.append((samples[j], samples[j + 1], samples[j + 2]))
        return out
    # Indexed. tRNS of 0 means fully transparent — skip so white canvas
    # behind a drop-shadow does not dilute a dark slide.
    pal_len = len(palette)
    for i in range(width):
        idx = samples[i]
        if idx >= pal_len:
            return None
        if idx < len(trans) and trans[idx] < 16:
            continue
        out.append(palette[idx])
    return out


def _unpack_samples(row: bytes, width: int, bit_depth: int, color_type: int) -> list[int] | None:
    spp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    need = width * spp
    if bit_depth == 8:
        if len(row) < need:
            return None
        return list(row[:need])
    # Packed 1/2/4-bit (palette or gray). MSB first per PNG.
    vals: list[int] = []
    maxv = (1 << bit_depth) - 1
    per_byte = 8 // bit_depth
    for byte in row:
        for shift in range(per_byte - 1, -1, -1):
            vals.append((byte >> (shift * bit_depth)) & maxv)
            if len(vals) >= need:
                return vals
    if len(vals) < need:
        return None
    return vals[:need]
