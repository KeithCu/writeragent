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

Stdlib PNG decode (not Pillow): the extension runs in LibreOffice's Python,
which typically has no PIL. A thumbnail whose width×height exceeds the pixel
cap is rejected before one RGB tuple is allocated per pixel (a few-KB
4096×4096 1-bit PNG is under the raw-byte cap but tens of millions of
tuples). ZIP member reads do not trust the declared uncompressed size:
``ZipFile.read`` with no length inflates up to ``ZipExtFile.MAX_N`` (~1 GiB)
before slicing to that size. Thumbnail, ``styles.xml``, and ``Pictures/*.svg``
are copied in small reads and dropped past ``_MAX_MEMBER_BYTES``. The
XML/SVG fallback also refuses to open every ``Pictures/*.svg`` or keep
every hex hit: a missing thumbnail must not turn listing into an
unbounded scan. 1/2/4-bit grayscale samples are scaled to 0–255 so an
all-white thumbnail stays a light background. We never load master pages
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
# ZipExtFile.read(n) passes n through as zlib max_length, then slices to
# the header file_size. One read(limit) still builds that whole buffer
# before the slice, so a forged file_size of 64 still costs ~limit bytes.
# Chunks keep each inflate request small; the loop enforces the real cap.
_ZIP_READ_CHUNK = 64 * 1024
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
    pics = [n for n in names if _norm_zip_name(n).startswith("pictures/") and not _norm_zip_name(n).endswith("/") and _norm_zip_name(n).rsplit("/", 1)[-1]]
    images = [n for n in pics if _norm_zip_name(n).endswith(_IMAGE_EXT)]
    if len(images) >= 2:
        return "illustrated"
    if len(images) == 1:
        # Single decorative SVG/PNG/etc. (Metropolis ships one chrome SVG).
        return "graphic chrome"
    return ""


def _read_member(zf: zipfile.ZipFile, name: str, limit: int = _MAX_MEMBER_BYTES) -> bytes | None:
    """Return one member's bytes, or None if missing, corrupt, or over *limit*.

    ``ZipInfo.file_size`` is not a read bound. That value is the
    uncompressed size in the central directory; the local file
    header stores the same field and both are forgeable.
    ``ZipExtFile.read`` with no size inflates via
    ``decompress(..., MAX_N)`` (~1 GiB) and only then slices to
    ``file_size``. A 64KB ``.otp`` whose declared size is 64 bytes
    still expands by ~100MB before a post-read cap can see the real
    length. ``list_designs`` → ``derive_otp_look`` does this on the
    main thread for every discovered template, including
    user-writable dirs (thumbnail, ``styles.xml``, and
    ``Pictures/*.svg``). A declared size above *limit* is skipped,
    but a small declared size is not the read bound. Chunks pass
    ``_ZIP_READ_CHUNK`` as zlib's ``max_length``, and the loop drops
    the member once more than *limit* bytes have been produced.
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    # Honest huge members are not opened. A forged *small* file_size
    # falls through; the sized loop below is what stops the inflate.
    if info.file_size > limit:
        return None
    buf = bytearray()
    try:
        with zf.open(info, "r") as src:
            while len(buf) <= limit:
                piece = src.read(_ZIP_READ_CHUNK)
                if not piece:
                    break
                if len(buf) + len(piece) > limit:
                    return None
                buf.extend(piece)
    except (zipfile.BadZipFile, EOFError, zlib.error):
        return None
    return bytes(buf)


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
    """Fallback when the thumbnail is missing: hex colors only, no invention.

    Opening every ``Pictures/*.svg`` (each already capped at
    ``_MAX_MEMBER_BYTES``) and appending every ``#RRGGBB`` makes
    ``_mood_and_accents`` walk that whole list. ``list_designs`` /
    ``apply_design`` call this on the main thread, including
    user-writable template dirs. Eight cap-sized SVG members are
    about 180MB and 12s; more members scale into gigabytes. The
    forged-size read cap limits one member, not how many members
    are opened or how many hex hits are kept. Read ``styles.xml``
    once and at most ``_MAX_XML_SVG_MEMBERS`` SVGs, and keep at
    most ``_MAX_XML_SAMPLES`` colors — the same budget as a
    downsampled thumbnail. Once that many hits are in hand,
    remaining members are not opened.
    """
    samples: list[tuple[int, int, int]] = []
    styles_name = ""
    svgs: list[str] = []
    for n in names:
        norm = _norm_zip_name(n)
        if norm == "styles.xml":
            if not styles_name:
                styles_name = n
        elif norm.startswith("pictures/") and norm.endswith(".svg") and len(svgs) < _MAX_XML_SVG_MEMBERS:
            svgs.append(n)
    to_read: list[str] = []
    if styles_name:
        to_read.append(styles_name)
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
    if limit <= 0:
        return out
    for match in _HEX_RE.finditer(blob):
        hex6 = match.group(1)
        alpha = match.group(2)
        if alpha is not None and int(alpha, 16) < 0x20:
            continue
        r = int(hex6[0:2], 16)
        g = int(hex6[2:4], 16)
        b = int(hex6[4:6], 16)
        out.append((r, g, b))
        if len(out) >= limit:
            break
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
    ranked = [pair for pair in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if pair[1] >= min_count]
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


def _inflate_capped(data: bytes, limit: int) -> bytes | None:
    """Inflate *data* or return None if output would pass *limit* bytes.

    ``zlib.decompress`` builds the whole output before the caller
    compares it to the IHDR size. A thumbnail whose declared size
    is small can still be a deflate bomb, and ``list_designs`` /
    ``apply_design`` would allocate that bomb before rejecting it.
    ``decompressobj.decompress(..., max_length)`` returns at most
    ``limit + 1`` bytes, so the oversize stream is dropped before a
    multi-megabyte buffer exists.
    """
    if limit < 0:
        return None
    dec = zlib.decompressobj()
    out = bytearray()
    pending: bytes | None = data
    try:
        while True:
            room = limit - len(out)
            if room < 0:
                return None
            chunk = dec.decompress(b"" if pending is None else pending, room + 1)
            pending = dec.unconsumed_tail or None
            if len(chunk) > room:
                return None
            out.extend(chunk)
            if dec.eof:
                return bytes(out)
            if pending is None and not chunk:
                return None
            if pending is None:
                pending = b""
    except zlib.error:
        return None


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
            if len(idat) > _MAX_MEMBER_BYTES:
                return None
        elif tag == b"IEND":
            break
    if width < 1 or height < 1 or width > 4096 or height > 4096:
        return None
    # After the raw-byte cap, building one RGB tuple per pixel still
    # blows up. A valid 4096×4096 1-bit thumbnail is a few KB and
    # under _MAX_RAW_PNG, then ~16.7M tuples (~1GB) before
    # _downsample. list_designs / apply_design feed
    # Thumbnails/thumbnail.png from discovered .otp files, including
    # the user-writable template dir. Reject on width*height before
    # inflate and before the pixel list. Mood falls back to
    # styles.xml / SVG hex colors.
    if width * height > _MAX_DECODED_PIXELS:
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
    raw = _inflate_capped(bytes(idat), expected)
    if raw is None:
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


def _unpack_row(row: bytes | bytearray, width: int, bit_depth: int, color_type: int, palette: list[tuple[int, int, int]], trans: bytes) -> list[tuple[int, int, int]] | None:
    samples = _unpack_samples(row, width, bit_depth, color_type)
    if samples is None:
        return None
    out: list[tuple[int, int, int]] = []
    if color_type == 0:
        # Bit depths 1/2/4 store samples in 0..maxv. Copying them as if
        # they were already 0..255 leaves an all-white 1-bit thumbnail
        # at sample 1, so luminance stays ~1 and _mood_and_accents
        # reports "dark background" for a white template. PNG scales a
        # grayscale sample with g * 255 / maxv (integer division). 8-bit
        # maxv is 255, so those samples are unchanged.
        maxv = (1 << bit_depth) - 1
        for i in range(width):
            g = samples[i] * 255 // maxv
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


def _unpack_samples(row: bytes | bytearray, width: int, bit_depth: int, color_type: int) -> list[int] | None:
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
