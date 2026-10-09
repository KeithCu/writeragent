# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Wire codec for Calc/chat data crossing the LO host (plain Python) and venv (NumPy).

Large 2D grids (numeric or mixed numeric-text) use Strategy 3 ``split_grid``: the entire
grid is serialized as a single contiguous double-precision flat float64 array (stored as raw
binary bytes) plus a parallel sparse integer-keyed strings dictionary. When the strings
dictionary is empty, NumPy in the child process ingests that via C-speed ``frombuffer`` +
``reshape`` — a direct zero-copy memory view over raw buffer bytes without any Python list/loop
transpositions or Base64 decoding overhead.

Adjust thresholds below if product policy changes; bench and production share this module.

Layout: policy helpers and envelope detectors, then pack/unpack. ``@deal``
contracts and ``# crosshair: off`` markers are intentional — see
docs/scripting/serialization-verification.md. Do not strip them, and do not
split pack/unpack without serialization A/B tests
(docs/scripting/numpy-serialization.md).

Depth caps differ on purpose. ``_MAX_UNPACK_DEPTH`` (128) bounds recursive
unpack, pack, and ``wire_cell_count``. It stays well under CPython's default
recursion limit (~1000) so a cycle raises ``ValueError`` here instead of
``RecursionError``.
``find_image_payloads`` stops at ``_MAX_IMAGE_DEPTH`` (12) because image
trees are shallow (past that it returns ``[]``). The venv
``_CUSTOM_SERIALIZE_MAX_DEPTH`` (8)
bounds custom-type walks and then treats the value as a plain container.
"""
from __future__ import annotations

import array
import logging
import math
import os
import sys
import tempfile
from typing import TYPE_CHECKING, Any, cast

from plugin.framework.deal_shim import (
    DEAL_MAX_COL_INDEX,
    DEAL_MAX_ROW_INDEX,
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SHAPE_RANK,
    DEAL_MAX_SOURCE,
    _profile,
    ascii_bounded,
    deal,
    inverse_ensure,
)

# CrossHair may invoke deal post/ensure as ``fn(*call_args, result=return_value, **kwargs)``.
# Naming a positional parameter ``result`` then raises TypeError (multiple values). Keep ``result`` keyword-only.
_DEAL_RETURN = object()
# Below the default recursion limit (~1000) so this ValueError is reached
# before RecursionError. No identity set on pack/unpack: that would skip a
# shared subtree (see find_image_payloads).
_MAX_UNPACK_DEPTH = 128
# Image trees are shallow. Deeper returns [] instead of raising, so a cyclic
# or huge result still finishes the walk. Not _MAX_UNPACK_DEPTH.
_MAX_IMAGE_DEPTH = 12
# host_unpack_data's contract, including OverflowError from int(inf) on an
# int column. venv_worker catches this same tuple so a bad envelope after
# the script has run is WORKER_IPC_ERROR, not a replay.
_HOST_UNPACK_ERRORS = (ValueError, OverflowError, TypeError, AttributeError, KeyError)


def _deal_return(*args: Any, result: Any = _DEAL_RETURN, **_kwargs: Any) -> Any:
    if result is not _DEAL_RETURN:
        return result
    return args[-1] if args else None


def _numpy_scalar_item(v: Any) -> Any:
    """Return ``v.item()`` for a NumPy scalar; otherwise ``v``.

    ``calc_range._materialize_inner_grid`` calls this for object-array cells.
    The import stays local so the host (LibreOffice Python, no NumPy) can
    import this module.
    """
    # crosshair: off  # numpy import sniff (same reason as _optional_numpy).
    try:
        import numpy as np  # local: safe on host; present in child for mixed grids
        if isinstance(v, np.generic):
            return v.item()
    except Exception:
        # numpy not present or v not a numpy scalar; fall through
        pass
    return v


def _to_py(v: Any) -> Any:
    """Recursively convert numpy scalars and nested sequences to native Python types.

    This is only reached for mixed-type (strings-present) child materialization paths.
    The import is local so the module can be imported on the host (LibreOffice's Python,
    which ships without NumPy).

    ``obj_arr.tolist()`` may already yield builtin scalars, which would make this
    walk redundant. Do not remove it without the serialization A/B suite
    (docs/scripting/numpy-serialization.md): dropping it is an unpack change.
    """
    # crosshair: off  # recursive list/tuple Any (cover-all 33355986432: payload_codec in-flight 6h with sandbox_cache, no flushed COVER TIMING). Doable later with _deal_envelope_value_ok.
    item = _numpy_scalar_item(v)
    if item is not v:
        return item
    if isinstance(v, (list, tuple)):
        return [_to_py(x) for x in v]
    return v


def _optional_numpy() -> Any:
    """Return the NumPy module, or None when this interpreter has no NumPy.

    ``child_pack_result``, ``_needs_elementwise_pack``,
    ``_container_has_packable_nested``, and ``_child_unpack_single_data``
    used to import NumPy before looking at the value. A plain dict or list
    then raised ``ImportError`` in a venv without NumPy (LibreOffice's Python
    ships without it, and some user venvs omit it). ``ModuleNotFoundError``
    is an ``ImportError``. Callers skip ndarray checks when this returns
    None. Numeric ``split_grid`` envelopes still import NumPy on their own.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    return np


if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

# --- Optional Cython accelerator --------------------------------------------------

_CYTHON_ACCELERATOR_DISABLED = False
_CYTHON_ACCELERATOR_LOCATION: str | None = None
# Set only after a load attempt fails. None means "not attempted yet" so
# report-only status stays "Inactive (Pure Python)" until the host loads.
_CYTHON_ACCELERATOR_INACTIVE_REASON: str | None = None

fast_flatten_grid_2d: Any = None
fast_flatten_grid_1d: Any = None


def _verify_accelerator(fn2d: Any, fn1d: Any) -> bool:
    """Perform a runtime canary test to ensure the Cython binary is correct and compatible."""
    # crosshair: off  # Any Cython callables (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Doable later with a closed canary fixture.
    try:
        if fn2d is None or fn1d is None:
            return False

        # 2D Test: [[1.0, None], ["text", 2.0]]
        test_2d = [[1.0, None], ["text", 2.0]]
        buf2, strings2, _unused, has_none2, non_num2 = fn2d(test_2d, 2)

        if not (
            len(buf2) == 4
            and buf2[0] == 1.0
            and math.isnan(buf2[1])
            and math.isnan(buf2[2])
            and buf2[3] == 2.0
            and strings2 == {2: "text"}
            and has_none2 == [False, True]
            and non_num2 is True
        ):
            log.warning("payload_codec: Cython 2D canary failed")
            return False

        # 1D Test: [1.0, "a", None]
        test_1d = [1.0, "a", None]
        buf1, strings1, _unused, has_none1, non_num1 = fn1d(test_1d)
        if not (
            len(buf1) == 3
            and buf1[0] == 1.0
            and math.isnan(buf1[1])
            and math.isnan(buf1[2])
            and strings1 == {1: "a"}
            and has_none1 == [True]
            and non_num1 is True
        ):
            log.warning("payload_codec: Cython 1D canary failed")
            return False

        # A plain int past ~1e308. float() raises OverflowError. An unguarded
        # PyFloat_AsDouble aborts the native call (_try_accel then hides it by
        # falling back) or, if the error is cleared and the -1.0 is kept,
        # stores a wrong number. The cell must be the decimal text.
        huge = 10**400
        neg = -huge
        buf_huge, strings_huge, _unused_huge, _none_huge, non_huge = fn1d([huge, neg])
        if not (
            len(buf_huge) == 2
            and math.isnan(buf_huge[0])
            and math.isnan(buf_huge[1])
            and strings_huge.get(0) == str(huge)
            and strings_huge.get(1) == str(neg)
            and non_huge is True
        ):
            log.warning("payload_codec: Cython huge-int canary failed")
            return False

        # dtype.kind "c" whose float() succeeds with only the real part.
        # No NumPy import: host load of this canary must stay stdlib-only.
        # A kind-"c" object that only fails float() would pass the old
        # PyFloat_AsDouble error path and hide the data-loss bug.
        class _CanaryComplexKind:
            dtype: Any = type("_CanaryDtype", (), {"kind": "c"})()

            def __float__(self) -> float:
                return 1.0

            def __str__(self) -> str:
                return "(1+2j)"

        cx = _CanaryComplexKind()
        buf_cx, strings_cx, _unused_cx, _none_cx, non_cx = fn2d([[cx, 1.0]], 2)
        if not (
            len(buf_cx) == 2
            and math.isnan(buf_cx[0])
            and buf_cx[1] == 1.0
            and strings_cx.get(0) == "(1+2j)"
            and non_cx is True
        ):
            log.warning("payload_codec: Cython complex-kind canary failed")
            return False

        return True
    except Exception as e:
        log.warning("payload_codec: Cython canary exception: %s", e)
        return False


def load_cython_accelerator() -> None:
    """Attempt to load the Cython accelerator and verify it via a runtime canary test.

    Host-only: the HTTP compute service calls this at startup to log Active/Inactive.
    Desktop host pack also calls it from ``host_pack_data``. Compute workers unpack
    via ``frombuffer`` / pack ndarrays via ``tobytes`` and must not call this
    (importing unpack helpers is not a load).

    ``=PY()`` can reach ``host_pack_data`` off the main thread (yellow bridge).
    Two first loads both bind the same canary-verified functions; the GIL makes
    that check-then-act idempotent. A lock would not cover an in-flight flatten
    during ``invalidate_host_cython_accelerator``. Do not add one unless
    invalidate must overlap a pack that is already running.
    """
    # crosshair: off  # sys.path/import sniffs (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Engine-hostile; keep off.
    global fast_flatten_grid_2d, fast_flatten_grid_1d
    global _CYTHON_ACCELERATOR_DISABLED, _CYTHON_ACCELERATOR_LOCATION, _CYTHON_ACCELERATOR_INACTIVE_REASON
    if fast_flatten_grid_2d is not None or _CYTHON_ACCELERATOR_DISABLED:
        return

    # Ensure native binary directories (installed user_config or in-tree repo contrib) are on sys.path
    try:
        from plugin.scripting.native_binaries import ensure_native_binaries_on_path

        ensure_native_binaries_on_path()
    except Exception as exc:
        log.debug("load_cython_accelerator path ensure exception: %s", exc)

    fn2d = None
    fn1d = None
    loc = "none"

    # Search import targets in priority order. These four layouts are real
    # (checkout, audio_binaries, bare sys.path, legacy plugin.contrib). Skip
    # unifying with native_binaries.py — easy to drop the accelerator.
    import importlib

    for mod_name in (
        "contrib.vec_pack",
        "writeragent_vec",
        "vec_pack",
        "plugin.contrib.vec_pack",
    ):
        if fn2d is not None and fn1d is not None:
            break
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        except Exception as exc:
            # A broken install must not abort the search. The first layout
            # already logged and continued; the later three only caught
            # ImportError and would have crashed host pack.
            log.warning("load_cython_accelerator exception importing %s: %s", mod_name, exc)
            continue
        candidate_2d = getattr(mod, "fast_flatten_grid_2d", None)
        candidate_1d = getattr(mod, "fast_flatten_grid_1d", None)
        if candidate_2d is not None and candidate_1d is not None:
            fn2d = candidate_2d
            fn1d = candidate_1d
            loc = mod_name

    # Perform runtime canary test before activating global state
    if fn2d is not None and fn1d is not None:
        if _verify_accelerator(fn2d, fn1d):
            fast_flatten_grid_2d = fn2d
            fast_flatten_grid_1d = fn1d
            _CYTHON_ACCELERATOR_LOCATION = loc
            _CYTHON_ACCELERATOR_DISABLED = False
            _CYTHON_ACCELERATOR_INACTIVE_REASON = None
            log.debug("payload_codec: Cython accelerator (%s) verified and loaded", loc)
        else:
            _CYTHON_ACCELERATOR_DISABLED = True
            _CYTHON_ACCELERATOR_LOCATION = None
            _CYTHON_ACCELERATOR_INACTIVE_REASON = "canary failed"
            log.warning("payload_codec: Cython accelerator found at %s but failed canary check; using pure Python", loc)
    else:
        _CYTHON_ACCELERATOR_DISABLED = True
        _CYTHON_ACCELERATOR_LOCATION = None
        _CYTHON_ACCELERATOR_INACTIVE_REASON = "not found"
        log.debug("payload_codec: Cython accelerator not found, using pure Python")


def invalidate_host_cython_accelerator() -> None:
    """Drop in-process accelerator state after host natives were replaced on disk.

    Redownload uses atomic replace (new inode), but ``sys.modules`` may still hold
    the old ``writeragent_vec`` module object. Clear globals and module cache so
    the next load binds the new file instead of calling into a stale mapping.
    """
    # crosshair: off  # sys.modules sniffs (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Engine-hostile; keep off.
    global fast_flatten_grid_2d, fast_flatten_grid_1d
    global _CYTHON_ACCELERATOR_DISABLED, _CYTHON_ACCELERATOR_LOCATION, _CYTHON_ACCELERATOR_INACTIVE_REASON
    fast_flatten_grid_2d = None
    fast_flatten_grid_1d = None
    _CYTHON_ACCELERATOR_DISABLED = False
    _CYTHON_ACCELERATOR_LOCATION = None
    _CYTHON_ACCELERATOR_INACTIVE_REASON = None
    for key in list(sys.modules):
        if key in ("writeragent_vec", "contrib.vec_pack", "vec_pack", "plugin.contrib.vec_pack") or key.startswith(
            ("writeragent_vec.", "contrib.vec_pack.", "vec_pack.", "plugin.contrib.vec_pack.")
        ):
            sys.modules.pop(key, None)


def reload_host_cython_accelerator() -> None:
    """Re-attempt loading the host-side Cython pack accelerator (main thread)."""
    global _CYTHON_ACCELERATOR_DISABLED
    _CYTHON_ACCELERATOR_DISABLED = False
    load_cython_accelerator()


def get_cython_status_info() -> tuple[bool, str | None, str]:
    """Return tuple of (is_active, source_location, status_line)."""
    if fast_flatten_grid_2d is not None:
        loc = _CYTHON_ACCELERATOR_LOCATION
        # Location is a module name (contrib.vec_pack, writeragent_vec, …) or
        # missing. It is never the literal "active".
        if loc:
            return True, loc, f"Cython Accelerator: Active (Optimized, source: {loc})"
        return True, loc, "Cython Accelerator: Active (Optimized)"
    if _CYTHON_ACCELERATOR_INACTIVE_REASON:
        return False, None, f"Cython Accelerator: Inactive (Pure Python; {_CYTHON_ACCELERATOR_INACTIVE_REASON})"
    return False, None, "Cython Accelerator: Inactive (Pure Python)"


def host_cython_status_line(*, reload: bool = False) -> str:
    """Human-readable host Cython status for Settings -> Python Test probe header.

    Default is report-only (no import/reload). Pass ``reload=True`` on the main
    thread after ``native_binaries.ensure_native_binaries_on_path`` when a fresh load is wanted.
    """
    if reload:
        reload_host_cython_accelerator()
    return get_cython_status_info()[2]


# Do not load at import. Compute workers import unpack helpers from this
# module; eager load would make every formula child pay for / claim Cython.
# Host paths call load_cython_accelerator() (HTTP startup, host_pack_data).

# --- Wire kind (JSON-safe dict tag) -----------------------------------------------

PAYLOAD_SPLIT_GRID = "split_grid"
"""Unified 2D grids: dense numeric flat float64 array and sparse strings dictionary."""

PAYLOAD_MULTI_DATA = "multi_data"
"""Multiple Calc ranges: list of split_grid or nested-list payloads."""

PAYLOAD_IMAGE = "image"
"""Matplotlib figure or other visualization serialized as SVG or PNG bytes."""

PAYLOAD_DATAFRAME = "dataframe"
"""Pandas DataFrame (or named Series) egress envelope: column labels + rectangular data grid.
The inner 'data' uses split_grid for large numeric/mixed rectangular results (same as plain arrays)
so that we avoid the expensive list-of-dicts records path while preserving column order/names."""

PAYLOAD_CALC_RANGE = "calc_range"
"""Ingress envelope: one rectangular Calc range. Inner ``data`` is list or split_grid.
User scripts see :class:`plugin.scripting.calc_range.CalcRange`, not the raw wire dict."""

# --- When to use binary envelope (default: at least 100 cells) -----------------------

BINARY_MIN_CELLS = 100
"""Use split_grid when total cell count is at least this."""

MAX_BENCH_CELLS = 100_000
"""Upper cap for benchmark grids (scripts/bench_serialization.py; production cap is scripting.python_max_data_cells)."""

ForceBinary = str
# Envelope ``dtype`` tag only. The buffer codec hardcodes array.array("d") and np.float64.
SPLIT_GRID_WIRE_DTYPE = "float64"
# array.array("d") / np.float64 item size. Buffer length must be a multiple of this.
_FLOAT64_BYTES = 8


def _is_grid_sequence(grid: object) -> bool:
    """True for empty, 1D, or 2D list/tuple grids (jagged 2D allowed; flatten raises ValueError)."""
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if not isinstance(grid, (list, tuple)):
        return False
    if len(grid) == 0:
        return True
    first = grid[0]
    if isinstance(first, (list, tuple)):
        return all(isinstance(row, (list, tuple)) for row in grid)
    return True


def _bounded_rect_grid_ok(grid: object, max_rows: int, max_cols: int) -> bool:
    """True for an empty, 1D, or rectangular-enough 2D list/tuple inside the caps.

    1D length uses ``max_rows`` (the outer sequence). A 2D row uses ``max_cols``.
    ``_deal_grid_ok`` and ``_deal_product_grid_ok`` are the same check with
    different caps; this is the one copy.
    """
    if not _is_grid_sequence(grid) or not isinstance(grid, (list, tuple)):
        return False
    if len(grid) > max_rows:
        return False
    if len(grid) == 0:
        return True
    first = grid[0]
    if isinstance(first, (list, tuple)):
        for row in grid:
            if not isinstance(row, (list, tuple)) or len(row) > max_cols:
                return False
    return True


def _deal_grid_ok(grid: object) -> bool:
    """CrossHair domain for list grids. Production ``_is_grid_sequence`` stays uncapped.

    Unbounded lists let deep check materialize huge nested grids in
    ``is_numeric_grid`` / pack. Side length follows ``DEAL_MAX_SHAPE_DIM``
    (pytest 256 still fits 100×100 pack-speed tests; CrossHair uses 4).
    """
    return _bounded_rect_grid_ok(grid, DEAL_MAX_SHAPE_DIM, DEAL_MAX_SHAPE_DIM)


def _deal_product_grid_ok(grid: object) -> bool:
    """Deal domain for live Calc→worker pack (``host_pack_*`` / flatten).

    ``_deal_grid_ok`` stays ``DEAL_MAX_SHAPE_DIM``-sized for CrossHair/small helpers.
    Product pack must accept real sheet ranges (Population A1:H1517 tripped the
    256-row SHAPE_DIM cap: PreContractError surfaced as =PY cell Error text).
    Caps at Calc sheet bounds (``DEAL_MAX_ROW_INDEX`` / ``DEAL_MAX_COL_INDEX``).
    """
    return _bounded_rect_grid_ok(grid, DEAL_MAX_ROW_INDEX + 1, DEAL_MAX_COL_INDEX + 1)


def _deal_numeric_cell_ok_pytest(value: object) -> bool:
    """Pytest/production: the detector is total.

    A list, a long string, or non-ASCII text used to raise PreContractError
    instead of False. The body already returns a bool. CrossHair keeps the
    scalar/ascii domain. ``value`` is unused.
    """
    return True


def _deal_numeric_cell_ok_crosshair(value: object) -> bool:
    """CrossHair domain for ``is_numeric_coercible``.

    Numpy scalars stay allowed in the body (``type(value).__name__``);
    the pre keeps SMT off ``Any`` + ``startswith``.
    """
    if value is None:
        return True
    t = type(value)
    if t is bool:
        return True
    if t is int or t is float:
        return True
    if t is str:
        return ascii_bounded(value, DEAL_MAX_SOURCE)
    return False


_deal_numeric_cell_ok = _profile(_deal_numeric_cell_ok_crosshair, _deal_numeric_cell_ok_pytest)


def _deal_wire_dict_ok_crosshair(obj: object) -> bool:
    """Short top-level key cap for the CrossHair table only.

    Real split_grid.strings maps have thousands of cell entries (Gemini AFC: 7588);
    do not deep-walk. Detectors are already crosshair: off. The short table still
    rejects a dict wider than SHAPE_DIM (4) so the domain stays closed.
    """
    if not isinstance(obj, dict):
        return True
    return len(obj) <= DEAL_MAX_SHAPE_DIM


def _deal_wire_dict_ok_pytest(obj: object) -> bool:
    """Pytest and production, including compute_service where deal stays installed.

    Envelope detectors are total predicates. The body returns False for a
    plain dict. The old shallow cap (len <= DEAL_MAX_SHAPE_DIM) raised
    PreContractError, which subclasses AssertionError, so ValueError handlers
    never saw it. host_unpack_data's @deal.pre calls the deal-wrapped
    _is_any_payload_envelope, so a word-frequency dict (>256 keys) died before
    the isinstance(dict) arm. json_egress is_* calls failed the same way.
    Release OXTs strip deal and already accepted these dicts. Do not deep-walk.
    ``obj`` is unused: every value, including a huge plain dict, is in domain.
    """
    return True


_deal_wire_dict_ok = _profile(_deal_wire_dict_ok_crosshair, _deal_wire_dict_ok_pytest)


def _is_multi_data_envelope(envelope: object) -> bool:
    # CrossHair TypeError on typing.Literal['a','b','rc'] when proxying empty dict (FV §8.1 D).
    # crosshair: off
    if not isinstance(envelope, dict):
        return False
    env_dict = cast("dict[str, Any]", envelope)
    if env_dict.get("__wa_payload__") != PAYLOAD_MULTI_DATA:
        return False
    items = env_dict.get("items")
    if not isinstance(items, list):
        return False
    return all(isinstance(item, (list, dict)) for item in items)


@deal.pre(lambda obj: _deal_wire_dict_ok(obj))
@deal.post(lambda result: isinstance(result, bool))
@inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_MULTI_DATA
        and isinstance(obj.get("items"), list)
    )
)
def is_multi_data(obj: Any) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    return _is_multi_data_envelope(obj)


def _is_image_payload_envelope(envelope: object) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if not isinstance(envelope, dict):
        return False
    env_dict = cast("dict[str, Any]", envelope)
    return (
        env_dict.get("__wa_payload__") == PAYLOAD_IMAGE
        and isinstance(env_dict.get("data"), bytes)
        and isinstance(env_dict.get("format"), str)
    )


@deal.pre(lambda obj: _deal_wire_dict_ok(obj))
@deal.post(lambda result: isinstance(result, bool))
@inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_IMAGE
        and isinstance(obj.get("data"), bytes)
        and isinstance(obj.get("format"), str)
    )
)
def is_image_payload(obj: Any) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    return _is_image_payload_envelope(obj)


def find_image_payloads(
    obj: Any,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Recursively find all image payloads in the object.

    Depth and an identity set stop a cyclic result from blowing the stack.
    The set is checked on containers, after an image payload is returned, so
    two keys pointing at the same image dict both count. A container already
    visited (a shared wrapper, or a cycle) is skipped, so that wrapper's
    images are reported once.
    """
    # crosshair: off  # recursive Any dict/list (cover-all 33355986432: payload_codec in-flight 6h with sandbox_cache, no flushed COVER TIMING). Doable later with _deal_envelope_value_ok.
    if _depth > _MAX_IMAGE_DEPTH:
        return []
    if is_image_payload(obj):
        return [obj]
    if isinstance(obj, (dict, list, tuple)):
        if _seen is None:
            _seen = set()
        marker = id(obj)
        if marker in _seen:
            return []
        _seen.add(marker)
        items = obj.values() if isinstance(obj, dict) else obj
        res = []
        for item in items:
            res.extend(find_image_payloads(item, _depth=_depth + 1, _seen=_seen))
        return res
    return []


def image_payload_suffix(payload: dict[str, Any]) -> str:
    """Return a temp-file suffix for *payload* (``.svg`` or ``.png``)."""
    # crosshair: off
    # cover-all 33797534946 (~46.5m payload_codec, 710 examples). Dict Any format probe. Doable later with closed format Literal.
    fmt = str(payload.get("format") or "png").lower()
    return ".svg" if fmt == "svg" else ".png"


def write_image_payload_to_temp(payload: dict[str, Any]) -> str:
    """Write image bytes from *payload* to a persistent temp file; return absolute path.

    The caller owns a successful path (Calc egress unlinks after GraphicURL
    loads). A failed write removes the file this function created.
    """
    # crosshair: off  # tempfile/filesystem (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Doable later with a bytes/format domain.
    suffix = image_payload_suffix(payload)
    path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            path = tmp.name
            tmp.write(payload["data"])
            return os.path.abspath(tmp.name)
    except Exception:
        # delete=False leaves the file when write raises (data was not bytes):
        # the with-block closes it and the exception propagates. This function
        # created the file, so a failed write must not leak it. A successful
        # path stays for the caller.
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise



def _is_dataframe_envelope(envelope: object) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if not isinstance(envelope, dict):
        return False
    env_dict = cast("dict[str, Any]", envelope)
    if env_dict.get("__wa_payload__") != PAYLOAD_DATAFRAME:
        return False
    cols = env_dict.get("columns")
    if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
        return False
    # Explicit ``data`` (including None) is required; missing key is not a DF envelope.
    if "data" not in env_dict:
        return False
    data = env_dict.get("data")
    # Accept list/tuple/dict (split_grid or nested), None, or ndarray.
    # Small numeric bodies are nested lists. An ndarray is still accepted so an
    # older child that skipped list egress still counts as a dataframe envelope.
    return isinstance(data, (list, tuple, dict)) or data is None or _is_ndarray(data)


@deal.pre(lambda obj: _deal_wire_dict_ok(obj))
@deal.post(lambda result: isinstance(result, bool))
@inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_DATAFRAME
        and isinstance(obj.get("columns"), list)
        and "data" in obj
    )
)
def is_dataframe_payload(obj: Any) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    return _is_dataframe_envelope(obj)


def _nonneg_shape_dim(d: object) -> bool:
    """True when a shape extent is a real non-negative int.

    ``bool`` is a subclass of ``int``, so ``isinstance(True, int)`` accepted
    ``[True, 1]`` as a calc_range or split_grid shape. Pack writes Python
    ints; a bare or legacy wire could pass a bool.
    """
    # crosshair: off
    if isinstance(d, bool) or not isinstance(d, int):
        return False
    return d >= 0


def _is_calc_range_envelope(envelope: object) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if not isinstance(envelope, dict):
        return False
    env_dict = cast("dict[str, Any]", envelope)
    if env_dict.get("__wa_payload__") != PAYLOAD_CALC_RANGE:
        return False
    shape = env_dict.get("shape")
    if not isinstance(shape, list) or len(shape) != 2:
        return False
    if not all(_nonneg_shape_dim(d) for d in shape):
        return False
    return "data" in env_dict


@deal.pre(lambda obj: _deal_wire_dict_ok(obj))
@deal.post(lambda result: isinstance(result, bool))
@inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_CALC_RANGE
        and isinstance(obj.get("shape"), list)
        and len(obj["shape"]) == 2
        and all(_nonneg_shape_dim(d) for d in obj["shape"])
        and "data" in obj
    )
)
def is_calc_range_payload(obj: Any) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    # Canonical wire guard. calc_range.py re-exports this; do not add a second copy.
    return _is_calc_range_envelope(obj)


def _is_split_grid_envelope(envelope: object) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if not isinstance(envelope, dict):
        return False
    env_dict = cast("dict[str, Any]", envelope)
    if env_dict.get("__wa_payload__") != PAYLOAD_SPLIT_GRID:
        return False
    shape = env_dict.get("shape")
    if not isinstance(shape, list) or len(shape) not in (1, 2):
        return False
    if not all(_nonneg_shape_dim(d) for d in shape):
        return False
    return isinstance(env_dict.get("buffer"), bytes) or isinstance(env_dict.get("b64"), str)


@deal.pre(lambda obj: _deal_wire_dict_ok(obj))
@deal.post(lambda result: isinstance(result, bool))
def _is_any_payload_envelope(obj: object) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    # Body already ORs the five family detectors; a matching ensure re-ran all
    # five on every CrossHair post (check-all deep 32900105768, 8:14).
    return (
        _is_split_grid_envelope(obj)
        or _is_multi_data_envelope(obj)
        or _is_image_payload_envelope(obj)
        or _is_dataframe_envelope(obj)
        or _is_calc_range_envelope(obj)
    )


def _is_ndarray(obj: object) -> bool:
    return type(obj).__name__ == "ndarray" and type(obj).__module__ == "numpy"


# Pytest accepts real sheets. ``_deal_grid_ok``'s 256-row cap raised
# PreContractError on a sheet pack accepts. CrossHair keeps that small domain.
_deal_column_kinds_grid_ok = _profile(_deal_grid_ok, _deal_product_grid_ok)


@deal.pre(lambda grid, *_unused, **__: _deal_column_kinds_grid_ok(grid))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), list))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(x in ("int", "float", "bool") for x in _deal_return(*a, result=result)))
@deal.raises(ValueError)
def column_kinds_for_grid(grid: list[Any] | list[list[Any]]) -> list[str]:
    """Policy helper (tests): per-column int/float/bool from source types; mirrors host_pack_split_grid."""
    # crosshair: off
    # ``except Exception: return []`` hid the jagged-grid ValueError that
    # flatten raises, and the small deal domain rejected a sheet pack accepts.
    # Call flatten directly. pytest uses the product-grid pre, and a jagged
    # grid raises.
    _unused, _unused2, kinds, _unused3 = _flatten_grid_to_components(grid)
    return kinds


def _uniform_column_kind(kinds: list[str]) -> str | None:
    """Return the kind when every column matches; else None (mixed columns)."""
    # crosshair: off
    # cover-all 33797534946 (~46.5m payload_codec, 634 examples). Combinatoric kinds list. Doable later with tiny kind alphabet.
    if not kinds:
        return None
    first = kinds[0]
    return first if all(k == first for k in kinds) else None


@deal.pre(
    lambda envelope, *_unused, ncols=0, **__: _deal_wire_dict_ok(envelope)
    and isinstance(ncols, int)
    # Same Calc column cap as ``_deal_product_grid_ok`` (pack). SHAPE_DIM (256)
    # rejected a wide sheet after pack succeeded: PreContractError on unpack.
    # Release strips deal, so the body already accepts this width; the pre must too.
    and 0 <= ncols <= DEAL_MAX_COL_INDEX + 1
)
def envelope_column_kinds(envelope: dict[str, Any], *, ncols: int) -> list[str]:
    """Per-column unpack kinds from wire ``column_kinds``."""
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    kinds = envelope.get("column_kinds")
    if isinstance(kinds, list) and len(kinds) == ncols:
        return ["int" if k == "int" else ("bool" if k == "bool" else "float") for k in kinds]
    return ["float"] * ncols


def envelope_uniform_column_kind(envelope: dict[str, Any], *, ncols: int) -> str | None:
    """Decode-only: all-int or all-float fast path when ``column_kinds`` are uniform; None if mixed."""
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    return _uniform_column_kind(envelope_column_kinds(envelope, ncols=ncols))


def _apply_column_kinds_to_ndarray(arr: Any, *, uniform: str | None) -> Any:
    """Cast a uniform int or bool float64 ndarray. Mixed or float stays float64.

    Callers pass ``uniform`` from ``_uniform_column_kind``. A 1-D grid has one
    column, so that value is ``int``, ``bool``, or ``float``. The old
    ``if is_1d`` arm re-read ``column_kinds[0]`` after that and never ran.
    """
    # crosshair: off  # numpy astype on Any (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Doable later with an ndarray/kinds domain.
    import numpy as np

    if uniform == "int":
        # Host unpack raises OverflowError on inf in an int column. astype(int64)
        # emits a garbage int and a RuntimeWarning. Pack never writes inf into
        # an int column; only a corrupt envelope does. Do not replace this
        # fast path with a Python int() loop to match the host.
        return arr.astype(np.int64)
    if uniform == "bool":
        # Host unpack treats only 1.0 as True. astype(bool) made every non-zero True.
        # NaN becomes False here: a bool ndarray cannot store NaN. Host unpack
        # keeps that NaN so a corrupt bool column still surfaces as a Calc error.
        # Do not np.where the NaN back in — that promotes a clean bool column to float64.
        return arr == 1.0
    # float, mixed, or empty kinds: stay float64. Casting mixed columns
    # one-by-one is a no-op (assignment coerces them back to float64).
    return arr


def _wire_cells_for_log(data: Any) -> str:
    """Cell count for a debug line.

    ``wire_cell_count`` raises ``ValueError`` on a cycle. This summary is
    called from ``except`` handlers; that error must not replace the caller's.
    """
    try:
        return str(wire_cell_count(data))
    except ValueError:
        return "?"


def describe_wire_value(obj: Any, *, sample: int = 3) -> str:
    """Short summary for debug logs (avoids dumping huge arrays or base64)."""
    # crosshair: off  # recursive Any walk (cover-all 33355986432: payload_codec in-flight 6h with sandbox_cache, no flushed COVER TIMING). Doable later with _deal_envelope_value_ok + sample bound.
    if is_image_payload(obj):
        return f"image format={obj.get('format')} bytes={len(obj.get('data', b''))}"
    if is_multi_data(obj):
        items = obj.get("items") or []
        return f"multi_data items={len(items)} cells={_wire_cells_for_log(obj)}"
    if is_split_grid(obj):
        buf = obj.get("buffer") or b""
        strings = obj.get("strings") or {}
        return (
            f"split_grid shape={obj.get('shape')} cells={_wire_cells_for_log(obj)} "
            f"column_kinds={obj.get('column_kinds')} strings={len(strings)} raw_bytes={len(buf)}"
        )
    if is_dataframe_payload(obj):
        cols = obj.get("columns") or []
        inner = obj.get("data")
        cells = _wire_cells_for_log(inner) if inner is not None else "0"
        return f"dataframe cols={len(cols)} cells~{cells}"
    if is_calc_range_payload(obj):
        shape = obj.get("shape")
        return f"calc_range shape={shape} cells={_wire_cells_for_log(obj)}"
    if obj is None:
        return "None"
    if isinstance(obj, (str, int, float, bool)):
        return f"{type(obj).__name__}={obj!r}"
    if isinstance(obj, dict):
        if "__wa_payload__" in obj:
            return f"dict(payload={obj.get('__wa_payload__')!r} keys={list(obj)})"
        keys = list(obj.keys())[:sample]
        return f"dict(keys={keys}{'…' if len(obj) > sample else ''})"
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        if n == 0:
            return "list[]"
        first = obj[0]
        if isinstance(first, (list, tuple)):
            # Be defensive: some list elements may not be rows (e.g. hypothesis fancier results with mixed nesting).
            try:
                ncols = max((len(r) for r in obj if isinstance(r, (list, tuple))), default=0)
                return f"list[{n}x{ncols}] sample_row={list(first)[:sample]!r}"
            except Exception:
                return f"list[{n}x?] sample_row={list(first)[:sample]!r}"
        return f"list[{n}] sample={list(obj)[:sample]!r}"
    return f"{type(obj).__name__}={repr(obj)[:120]}"


def _deal_shape_ok_pytest(shape: object) -> bool:
    """Wide Calc-sized shape domain for pytest / production deal checks."""
    max_dim = DEAL_MAX_ROW_INDEX + 1
    return (
        isinstance(shape, tuple)
        and len(shape) <= DEAL_MAX_SHAPE_RANK
        and all(isinstance(d, int) and 0 <= d <= max_dim for d in shape)
    )


def _deal_shape_ok_crosshair(shape: object) -> bool:
    """Tiny shape domain for CrossHair.

    cover-all 35546602462 spent ~29m on ``should_use_binary_envelope`` /
    ``cell_count`` despite ``DEAL_MAX_ROW_INDEX=20``. Keep the FQNs on with a
    2×{0..4} grid instead of off.
    """
    return (
        isinstance(shape, tuple)
        and len(shape) <= 2
        and all(isinstance(d, int) and 0 <= d <= 4 for d in shape)
    )


_deal_shape_ok = _profile(_deal_shape_ok_crosshair, _deal_shape_ok_pytest)


@deal.pre(lambda shape: _deal_shape_ok(shape))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), int))
@deal.ensure(lambda shape, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) >= 0)
@deal.ensure(lambda shape, *a, result=_DEAL_RETURN, **k: len(shape) != 0 or _deal_return(*a, result=result) == 1)
def cell_count(shape: tuple[int, ...]) -> int:
    n = 1
    for d in shape:
        n *= d
    return n


def _deal_force_min_cells_domain(min_cells: object, force: object, *, max_cells: int) -> bool:
    """Shared ``force`` / ``min_cells`` domain for policy and pack contracts.

    ``should_use_binary_envelope``, ``binary_envelope_skip_reason``,
    ``host_pack_data``, and ``host_pack_multi_data`` used to repeat this
    check. Each ``@deal.pre`` stays a keyword-default lambda so CrossHair's
    call shape still matches. The cap is chosen once via ``_profile``:
    CrossHair keeps ``DEAL_MAX_SHAPE_DIM`` so the search domain stays small.
    Pytest allows a cell-count threshold up to one full Calc column
    (``DEAL_MAX_ROW_INDEX + 1``). ``DEAL_MAX_SHAPE_DIM`` (256) is a side
    length; it rejected ``scripts/bench_serialization.py --min-cells 1000``.
    """
    return (
        force in ("auto", "always", "never")
        and isinstance(min_cells, int)
        and 0 <= min_cells <= max_cells
    )


def _deal_force_min_cells_ok_crosshair(min_cells: object, force: object) -> bool:
    return _deal_force_min_cells_domain(min_cells, force, max_cells=DEAL_MAX_SHAPE_DIM)


def _deal_force_min_cells_ok_pytest(min_cells: object, force: object) -> bool:
    return _deal_force_min_cells_domain(min_cells, force, max_cells=DEAL_MAX_ROW_INDEX + 1)


_deal_force_min_cells_ok = _profile(
    _deal_force_min_cells_ok_crosshair, _deal_force_min_cells_ok_pytest
)


@deal.pre(lambda shape, *_unused, **__: _deal_shape_ok(shape))
@deal.pre(
    lambda shape, *_unused, min_cells=BINARY_MIN_CELLS, force="auto", **__: _deal_force_min_cells_ok(
        min_cells, force
    )
)
# CrossHair may pass call args + result=; never bind ``result`` as a positional parameter.
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), bool))
@deal.ensure(lambda *a, result=_DEAL_RETURN, force="auto", **k: force != "always" or _deal_return(*a, result=result) is True)
@deal.ensure(lambda *a, result=_DEAL_RETURN, force="auto", **k: force != "never" or _deal_return(*a, result=result) is False)
def should_use_binary_envelope(
    shape: tuple[int, ...],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> bool:
    """Return True if policy says pack data as split_grid instead of JSON lists."""
    if force == "always":
        return True
    if force == "never":
        return False
    # ``bool(shape)`` on a CrossHair symbolic tuple returns SymbolicBool;
    # Python's ``and`` then TypeErrors (``__bool__`` must return bool) —
    # should_use_binary_envelope((), min_cells=0, force='auto') on check-all
    # deep 32900105768. Empty tuple is still False via len; keep this FQN on.
    return len(shape) > 0 and cell_count(shape) >= min_cells


@deal.pre(lambda shape, *_unused, **__: _deal_shape_ok(shape))
@deal.pre(
    lambda shape, *_unused, min_cells=BINARY_MIN_CELLS, force="auto", **__: _deal_force_min_cells_ok(
        min_cells, force
    )
)
def binary_envelope_skip_reason(
    shape: tuple[int, ...],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> str | None:
    """Human-readable reason split_grid was not used; None if envelope would be used."""
    # crosshair: off
    # cover-all 33797534946 (~46.5m payload_codec, 937 examples). Combinatoric skip-reason strings. Keep should_use_binary_envelope on (_CROSSHAIR_TARGETS). Doable later with closed force/shape domain.
    if should_use_binary_envelope(shape, min_cells=min_cells, force=force):
        return None
    if force == "never":
        return "force=never"
    ncells = cell_count(shape)
    return f"needs cells >= {min_cells} (got {ncells} in shape {shape})"


def _is_numeric_coercible_impl(value: Any) -> bool:
    """Body of ``is_numeric_coercible`` without ``@deal.pre`` (used by ``is_numeric_grid``)."""
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if value is None or isinstance(value, (bool, int, float)):
        return True
    # NumPy scalars (int64, float64, bool_, uint64) without a module-level import.
    # A type whose name starts with int/float/bool/uint (a user class
    # ``internal``) is not numeric. Only numpy scalar classes take that
    # shortcut; builtins already returned above.
    tname = type(value).__name__
    mod = getattr(type(value), "__module__", "")
    if (mod == "numpy" or (isinstance(mod, str) and mod.startswith("numpy."))) and tname.startswith(
        ("int", "float", "bool", "uint")
    ):
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


@deal.pre(lambda value: _deal_numeric_cell_ok(value))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), bool))
@deal.ensure(
    lambda value, *a, result=_DEAL_RETURN, **k: not (isinstance(value, str) and value.strip())
    or _deal_return(*a, result=result) is False
)
@deal.ensure(lambda value, *a, result=_DEAL_RETURN, **k: value is not None or _deal_return(*a, result=result) is True)
@deal.ensure(
    lambda value, *a, result=_DEAL_RETURN, **k: not isinstance(value, (bool, int, float))
    or _deal_return(*a, result=result) is True
)
def is_numeric_coercible(value: Any) -> bool:
    """True when a cell may enter the numeric-grid / ``np.array(list)`` gate.

    The name is that gate, not "``float()`` succeeds". Non-empty strings are
    never coercible here — even ``\"02138\"`` parses as a float — so mixed
    grids stay lists after child split_grid unpack (zip codes and labels
    preserved). Empty and whitespace strings return True because they match
    Calc blanks. ``np.array(..., dtype=float64)`` still raises ``ValueError``
    on them, and ``_child_unpack_single_data`` keeps the list in that case.
    """
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    return _is_numeric_coercible_impl(value)


@deal.pre(lambda grid: isinstance(grid, list) and _deal_product_grid_ok(grid))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), bool))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: len(grid) > 0 or _deal_return(*a, result=result) is True)
def is_numeric_grid(grid: list[Any] | list[list[Any]]) -> bool:
    """True when every cell is numeric-coercible (safe for numeric-only split_grid fast-path)."""
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if len(grid) == 0:
        return True
    if type(grid[0]) in (list, tuple):
        # ``for cell in row`` raises TypeError when a later row is an int.
        # Deal's pre rejects that grid; a release build strips deal, so the
        # body used to TypeError. ``_split_grid_row_width`` raises ValueError
        # unless the row is a list or tuple, same as host pack.
        for row in grid:
            _split_grid_row_width(row)
        return all(_is_numeric_coercible_impl(cell) for row in grid for cell in row)
    return all(_is_numeric_coercible_impl(cell) for cell in grid)


@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), int) and _deal_return(*a, result=result) >= 0)
@deal.ensure(lambda data, *a, result=_DEAL_RETURN, **k: data is not None or _deal_return(*a, result=result) == 0)
@deal.raises(ValueError)
def wire_cell_count(data: Any, *, _depth: int = 0) -> int:
    """Cell count for size limits; works on lists or split_grid / multi_data / calc_range envelopes."""
    # crosshair: off
    # Envelope detectors + typed payload tags hit CrossHairInternal/Literal proxy errors on garbage dicts.
    # A self-referential multi_data walked until RecursionError, and
    # describe_wire_value then failed its deal post with TypeError. Use the
    # same depth cap as host_unpack_data. No identity set: a shared subtree
    # is still counted; a cycle hits 128 and raises.
    if _depth > _MAX_UNPACK_DEPTH:
        raise ValueError("payload_codec: wire_cell_count maximum recursion depth exceeded")
    if is_calc_range_payload(data):
        shape = data.get("shape") or [0, 0]
        if isinstance(shape, list) and len(shape) == 2:
            return int(shape[0]) * int(shape[1])
        return wire_cell_count(data.get("data"), _depth=_depth + 1)
    if is_multi_data(data):
        items = data.get("items") or []
        return sum(wire_cell_count(item, _depth=_depth + 1) for item in items)
    if is_split_grid(data):
        return cell_count(tuple(int(x) for x in data["shape"]))
    if is_dataframe_payload(data):
        return wire_cell_count(data.get("data"), _depth=_depth + 1)
    if data is None:
        return 0
    if type(data) not in (list, tuple):
        # A dataframe whose data is an ndarray is not a list. Returning 1 made
        # describe_wire_value log cells~1. Host size guards only see
        # host-packed lists; this is the debug count. _is_ndarray does not
        # import NumPy. .size is rows*cols.
        if _is_ndarray(data):
            return int(data.size)
        return 1
    if not data:
        return 0
    first = data[0]
    if type(first) in (list, tuple):
        # Same non-sequence row as is_numeric_grid: len() on a later int
        # raised TypeError, and this function has no rectangular pre.
        return sum(_split_grid_row_width(row) for row in data)
    return len(data)


@deal.pre(lambda grid: isinstance(grid, list) and _deal_product_grid_ok(grid))
@deal.post(lambda result: isinstance(result, list))
def grid_from_nested_list(grid: list[Any] | list[list[Any]]) -> list[Any] | list[list[Any]]:
    """Normalize to flat or 2D Python lists for small grids (below BINARY_MIN_CELLS) or non-split_grid results."""
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    if len(grid) == 0:
        return []
    if type(grid[0]) in (list, tuple) and all(isinstance(r, (list, tuple)) for r in grid):
        return [[_cell_for_json(c) for c in row] for row in grid]
    return [_cell_for_json(x) for x in grid]


def _cell_for_json(value: Any) -> Any:
    """Normalize a single egress cell for list paths.

    Python None (from mixed/text results or explicit) becomes None (later mapped to empty cell in Calc).
    float('nan') / np.nan is preserved so it surfaces as a Calc error (cascades) rather than a silent blank.
    This applies to small grids (< BINARY_MIN_CELLS) and list results that do not use the split_grid envelope.

    Named on purpose. Do not inline it into ``grid_from_nested_list``: this is
    the list-path hook that must not start rewriting NaN. The body is a
    placeholder: ``None`` stays ``None``, and every other value is returned
    unchanged.
    """
    if value is None:
        return None
    return value


def _flatten_update_column_state(column_states: list[int], c: int, val: Any) -> None:
    """Upgrade per-column numeric kind after a successful float(val) on the fast path.

    Column state, shared with Cython ``_update_column_state`` (do not merge
    the loops; the fast/slow split is the perf design):
    ``0`` empty, ``1`` bool, ``2`` int, ``3`` float.
    bool then int promotes to int (``True`` and ``1`` are both ``1.0``).
    Any float promotes to float and stays there. Object, Decimal, and any
    other numeric that already survived ``float()`` become float.
    """
    # crosshair: off  # Any val sibling of already-off flatten (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Doable later with a tiny cell domain.
    st = column_states[c]
    if st == 3:
        return
    # One kind per column. An int promotes bool (state 1 → 2): True and 1 are
    # both 1.0 in the float64 buffer, so host int() and child astype(int64)
    # turn True into 1. force="never" keeps the Python bool.
    # A per-cell bool mask was rejected. It would be a second side channel
    # beside strings, and the Cython flattener would have to emit the same
    # mask. Adjacent columns still keep their own kinds.
    if val is True or val is False:
        if st == 0:
            column_states[c] = 1
        return
    tv = type(val)
    if tv is float:
        column_states[c] = 3
        return
    if tv is int:
        if st < 2:
            column_states[c] = 2
        return
    dtype = getattr(val, "dtype", None)
    if dtype is not None:
        kind = getattr(dtype, "kind", None)
        if kind == "f":
            column_states[c] = 3
        elif kind in ("i", "u"):
            if st < 2:
                column_states[c] = 2
        elif kind == "b":
            if st == 0:
                column_states[c] = 1
        else:
            # Kind "O" (and any other non f/i/u/b) must still write state.
            # Returning without writing left a bool/int column as bool/int on
            # the pure path and float when the accelerator loaded. Cython
            # ``_update_column_state`` sets state 3 for that else. float(val)
            # already succeeded before this helper runs.
            column_states[c] = 3
        return
    tname = tv.__name__
    if tname.startswith("bool"):
        if st == 0:
            column_states[c] = 1
    elif tname.startswith(("int", "uint")):
        if st < 2:
            column_states[c] = 2
    elif tname.startswith("float"):
        column_states[c] = 3
    else:
        # Decimal/Fraction/etc. already survived float(val). Default to float,
        # matching Cython _update_column_state — not int (state 0).
        column_states[c] = 3


class _AsText:
    """Marker: this cell is text, not a float64 buffer value."""


_AS_TEXT = _AsText()


def _numeric_cell_to_float(val: Any) -> float | _AsText:
    """Convert one cell to float64, or ``_AS_TEXT`` when it must be ``str(val)``.

    The fast path and ``_flatten_append_cell_slow`` each called ``float()``
    on a plain ``int`` with no guard, so ``10**400`` raised ``OverflowError``
    (``RaisesContractError`` under deal, a worker failure once deal is
    stripped). ``float(np.complex64/128/clongdouble)`` succeeds and keeps only
    the real part. Decimal and Fraction already caught ``OverflowError``.
    Python ``complex`` already failed ``float()`` and was stored as text.
    Both Python flatten lanes call this helper. Cython ``_flatten_cell``
    mirrors the int-overflow and ``dtype.kind == "c"`` checks; do not merge
    the loops. Integers that ``float()`` accepts, including those past the
    53-bit mantissa, stay numeric.
    """
    # crosshair: off  # Any cell; sibling of the already-off flatten loops.
    tv = type(val)
    if tv is float:
        return val
    if tv is bool:
        return float(val)
    if tv is int:
        try:
            return float(val)
        except OverflowError:
            return _AS_TEXT
    # isinstance(val, complex) is true for np.complex128 only. complex64 and
    # clongdouble are not builtin complex, and their float() drops the
    # imaginary part with a ComplexWarning instead of raising.
    dtype = getattr(val, "dtype", None)
    if dtype is not None and getattr(dtype, "kind", None) == "c":
        return _AS_TEXT
    if tv is complex:
        return _AS_TEXT
    try:
        return float(val)
    except (TypeError, ValueError, OverflowError):
        return _AS_TEXT


def _store_numeric_cell(
    cell: Any,
    idx: int,
    *,
    buf_append: Any,
    strings: dict[int, str],
    nan: float,
) -> bool:
    """Write one float64 cell. Return False when the cell was stored as text.

    Module-level so the slow path does not allocate this helper on every text
    cell. ``_flatten_append_cell_slow`` used to nest it and rebuild it per call.
    """
    # crosshair: off  # Any cell; sibling of the already-off flatten loops.
    fval = _numeric_cell_to_float(cell)
    if fval is _AS_TEXT:
        buf_append(nan)
        strings[idx] = str(cell)
        return False
    buf_append(fval)
    return True


def _flatten_append_cell_slow(
    val: Any,
    c: int,
    idx: int,
    *,
    buf_append: Any,
    strings: dict[int, str],
    column_states: list[int],
    column_has_none: list[bool],
    nan: float,
) -> None:
    """Full per-cell flatten semantics (None, strings, NumPy scalars, column metadata)."""
    # crosshair: off  # Any val sibling of already-off flatten (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Doable later with a tiny cell domain.

    if val is None:
        buf_append(nan)
        column_has_none[c] = True
    elif val is True or val is False:
        buf_append(float(val))
        if column_states[c] == 0:
            column_states[c] = 1
    elif type(val) is int:
        # Huge ints are text. float(10**400) used to raise out of this branch.
        if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan) and column_states[c] < 2:
            column_states[c] = 2
    elif type(val) is float:
        buf_append(val)
        column_states[c] = 3
    else:
        t = type(val)
        dtype = getattr(val, "dtype", None)
        if dtype is not None:
            kind = getattr(dtype, "kind", None)
            if kind == "f":
                if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan):
                    column_states[c] = 3
            elif kind in ("i", "u"):
                if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan) and column_states[c] < 2:
                    column_states[c] = 2
            elif kind == "b":
                if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan) and column_states[c] == 0:
                    column_states[c] = 1
            else:
                # np.str_ subclasses str and Cython PyUnicode_Check stores it
                # before trying float. Storing it raw fails host unpickle
                # (LibreOffice Python has no NumPy). str() yields a builtin str.
                # Other unknown kinds (dtype "O" with __float__) must not be
                # stringified here, or the same value is 1.5 before a text
                # cell and "W" after one. Cython PyFloat_AsDouble keeps the
                # float. Kind "c" is the exception: float() keeps the real
                # part, so _numeric_cell_to_float stringifies it.
                if isinstance(val, str):
                    buf_append(nan)
                    strings[idx] = str(val)
                elif _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan) and column_states[c] != 3:
                    _flatten_update_column_state(column_states, c, val)
            return
        tname = t.__name__
        if tname.startswith("bool"):
            if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan) and column_states[c] == 0:
                column_states[c] = 1
        elif tname.startswith(("int", "uint")):
            if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan) and column_states[c] < 2:
                column_states[c] = 2
        elif tname.startswith("float"):
            if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan):
                column_states[c] = 3
        elif not isinstance(val, str):
            # The fast path and Cython ``_flatten_cell`` float() a Decimal or
            # Fraction. This branch runs only after an earlier cell set
            # has_non_numeric, and str() on those values made the same number
            # 1.25 or the text "1.25" / "1/4" depending on position. decimal
            # and fractions are on the venv import whitelist, and a pandas
            # object column reaches this flatten without the pickle-leaf
            # coerce. Strings stay text (zip codes). A value ``float()``
            # rejects (huge Fraction, Python complex) is text via
            # ``_numeric_cell_to_float``.
            if _store_numeric_cell(val, idx, buf_append=buf_append, strings=strings, nan=nan) and column_states[c] != 3:
                _flatten_update_column_state(column_states, c, val)
        else:
            buf_append(nan)
            # Same as the dtype branch above: plain str, not np.str_.
            strings[idx] = str(val)


def _split_grid_row_width(row: Any) -> int:
    """Width of one 2D row.

    ``len(row)`` on a later int raises ``TypeError`` once deal is stripped
    (release / LibreOffice). Pack only declares ``ValueError``. A real row is
    a list or tuple. ``str`` has a length but is not a row (``"ab"`` would
    otherwise look two cells wide).
    """
    # crosshair: off  # malformed row (Any), sibling of already-off grid validate.
    if not isinstance(row, (list, tuple)):
        raise ValueError(f"split_grid row is {type(row).__name__}, not a list or tuple")
    return len(row)


def _validate_rectangular_grid(grid_2d: list[list[Any]]) -> int:
    """Column count of a rectangular 2D grid.

    Shape used to take ``len`` of the first row, then a second walk compared
    every row to that width. One walk rejects a non-row and a jagged width
    and returns the width. A release build has no deal pre on this path.
    """
    # crosshair: off  # unbounded 2D grid (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Doable later with _deal_grid_ok.
    if not grid_2d:
        return 0
    ncols = _split_grid_row_width(grid_2d[0])
    for row in grid_2d[1:]:
        if _split_grid_row_width(row) != ncols:
            row_lens = [_split_grid_row_width(r) for r in grid_2d]
            log.error("payload_codec: uneven row lengths in 2D grid: %s", row_lens)
            raise ValueError(f"Uneven row lengths in data grid: {row_lens}")
    return ncols


def _iter_split_grid_cells(
    grid: list[Any] | list[list[Any]],
    *,
    is_2d: bool,
) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(col_idx, flat_idx, val)`` row-major for 1D or validated 2D grids."""
    # crosshair: off  # unbounded grid walk (cover-all 33355986432: payload_codec in-flight 6h, no flushed COVER TIMING). Doable later with _deal_grid_ok.
    if is_2d:
        grid_2d = cast("list[list[Any]]", grid)
        idx = 0
        for row in grid_2d:
            for c, val in enumerate(row):
                yield c, idx, val
                idx += 1
        return
    grid_1d = cast("list[Any]", grid)
    for idx, val in enumerate(grid_1d):
        yield 0, idx, val


@deal.pre(lambda grid: _deal_product_grid_ok(grid))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and isinstance(r, tuple) and len(r) == 4 and isinstance(r[0], array.array) and isinstance(r[1], dict) and isinstance(r[2], list) and isinstance(r[3], list))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and (not grid) == (len(r[0]) == 0 and r[1] == {} and r[2] == [] and r[3] == [0]))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(isinstance(key, int) for key in _deal_return(*a, result=result)[1].keys()))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and len(r[2]) == (0 if not grid else (r[3][1] if len(r[3]) == 2 else 1)))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(isinstance(v, str) for v in _deal_return(*a, result=result)[1].values()))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(kind in ("int", "float", "bool") for kind in _deal_return(*a, result=result)[2]))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and ((not grid) or len(r[0]) == (r[3][0] * r[3][1] if len(r[3]) == 2 else r[3][0])))
@deal.raises(ValueError)
def _flatten_grid_to_components(
    grid: list[Any] | list[list[Any]]
) -> tuple[array.array[float], dict[int, str], list[str], list[int]]:
    """Flatten 1D/2D grid to float64 array, strings dict, column kinds, and shape."""
    # crosshair: off
    if not grid:
        return array.array("d"), {}, [], [0]

    first = grid[0]
    is_2d = type(first) in (list, tuple)
    if is_2d:
        grid_2d = cast("list[list[Any]]", grid)
        nrows = len(grid_2d)
        # One walk: width and the rectangular check. The accelerator below
        # must not see a jagged grid.
        ncols = _validate_rectangular_grid(grid_2d)
        shape = [nrows, ncols]
    else:
        nrows = 1
        ncols = len(grid)
        shape = [ncols]

    buf = array.array("d")
    strings: dict[int, str] = {}
    # Bound to this array before the accelerator may rebind ``buf``. Stdlib
    # fallback appends here; do not move this capture below that block.
    buf_append = buf.append
    nan = math.nan

    # --- Fast path setup -------------------------------------------------
    num_cols = ncols if is_2d else 1
    column_states = [0] * num_cols          # 0=None, 1=bool, 2=int, 3=float
    column_has_none = [False] * num_cols
    has_non_numeric = False

    def _append_cell_slow(val: Any, c: int, idx: int) -> None:
        _flatten_append_cell_slow(
            val,
            c,
            idx,
            buf_append=buf_append,
            strings=strings,
            column_states=column_states,
            column_has_none=column_has_none,
            nan=nan,
        )

    def _stdlib_flatten_pass(cell_iter: Iterator[tuple[int, int, Any]]) -> None:
        nonlocal has_non_numeric
        for c, idx, val in cell_iter:
            t = type(val)
            if val is None:
                buf_append(nan)
                column_has_none[c] = True
            elif isinstance(val, str):
                has_non_numeric = True
                _append_cell_slow(val, c, idx)
            elif not has_non_numeric:
                if t is float:
                    buf_append(val)
                    if column_states[c] != 3:
                        column_states[c] = 3
                elif t is int:
                    # float(10**400) raises. The slow helper stringifies it.
                    fval = _numeric_cell_to_float(val)
                    if fval is _AS_TEXT:
                        has_non_numeric = True
                        _append_cell_slow(val, c, idx)
                    else:
                        # ``is`` does not narrow ``float | _AsText`` for ty/basedpyright.
                        buf_append(cast("float", fval))
                        if column_states[c] < 2:
                            column_states[c] = 2
                elif val is True or val is False:
                    buf_append(float(val))
                    if column_states[c] == 0:
                        column_states[c] = 1
                else:
                    fval = _numeric_cell_to_float(val)
                    if fval is _AS_TEXT:
                        has_non_numeric = True
                        _append_cell_slow(val, c, idx)
                    else:
                        buf_append(cast("float", fval))
                        if column_states[c] != 3:
                            _flatten_update_column_state(column_states, c, val)
            else:
                _append_cell_slow(val, c, idx)

    # Mostly-numeric Calc grids: try float(val) until non-numeric forces slow path.
    # None is handled in the fast path to avoid disabling it for empty cells.
    def _try_accel(fn: Any, call_args: tuple[Any, ...], expected: int, *, label: str) -> bool:
        """Bind accelerator output only when the buffer length matches the shape.

        Both ranks used to duplicate this try/except. A length mismatch used to
        be easy to update in one branch and not the other. A short native
        buffer falls back to stdlib (buf_append still targets the original array).
        """
        nonlocal buf, strings, column_states, column_has_none, has_non_numeric
        try:
            accel_buf, accel_strings, accel_states, accel_none, accel_non = fn(*call_args)
            if len(accel_buf) != expected:
                raise ValueError(
                    f"accelerator returned {len(accel_buf)} cells, shape needs {expected}"
                )
            buf = accel_buf
            strings = accel_strings
            column_states = accel_states
            column_has_none = accel_none
            has_non_numeric = accel_non
            return True
        except Exception as exc:
            log.debug("payload_codec: Cython %s failed, falling back to stdlib: %s", label, exc)
            return False

    if is_2d:
        grid_2d = cast("list[list[Any]]", grid)
        # Rectangular check already ran in _validate_rectangular_grid, before this
        # accelerator. It used to run only on the stdlib branch, so a native
        # packer could pad a jagged grid and skip the ValueError. A short or
        # long accelerator buffer still raises inside the try so the except
        # falls back to stdlib (buf_append still targets the original buffer).
        use_stdlib = True
        if fast_flatten_grid_2d is not None:
            rows = [list(row) if type(row) is tuple else row for row in grid_2d]
            use_stdlib = not _try_accel(
                fast_flatten_grid_2d, (rows, ncols), nrows * ncols, label="accelerator"
            )

        if use_stdlib:
            _stdlib_flatten_pass(_iter_split_grid_cells(grid_2d, is_2d=True))
    else:
        grid_1d = cast("list[Any]", grid)
        use_stdlib = True
        if fast_flatten_grid_1d is not None:
            use_stdlib = not _try_accel(
                fast_flatten_grid_1d, (grid_1d,), len(grid_1d), label="1D accelerator"
            )

        if use_stdlib:
            _stdlib_flatten_pass(_iter_split_grid_cells(grid_1d, is_2d=False))

    # Map the final column states to "int" / "float" / "bool" with single-pass promotions
    column_kinds: list[str] = []
    for c in range(num_cols):
        state = column_states[c]
        if state == 3:
            kind = "float"
        elif state == 1:
            kind = "bool"
        elif state == 0:
            # No numeric cell was seen (text-only, or blanks beside text).
            # "int" was a lie for any consumer that trusts the tag. Decode
            # still prefers the strings map, so cell values do not change.
            kind = "float"
        else:
            kind = "int"

        # If purely numeric grid (strings is empty), any column with None must be promoted to "float"
        # to avoid NumPy casting errors on NaN values.
        if not strings and column_has_none[c]:
            kind = "float"

        column_kinds.append(kind)

    # The Cython accelerator can leave np.str_ in the map (it is a str subclass).
    # Host unpickle has no NumPy. The stdlib path already stores builtin str;
    # this only rewrites a map that still holds a subclass.
    if strings and any(type(val) is not str for val in strings.values()):
        strings = {idx: str(val) for idx, val in strings.items()}

    return buf, strings, column_kinds, shape


@deal.pre(lambda grid: _deal_product_grid_ok(grid))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), dict))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("__wa_payload__") == PAYLOAD_SPLIT_GRID)
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("dtype") == SPLIT_GRID_WIRE_DTYPE)
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("buffer"), bytes))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("strings"), dict))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: all(isinstance(key, int) for key in _deal_return(*a, result=result).get("strings", {})))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("column_kinds"), list))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("shape"), list))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and (len(r["buffer"]) == 0 if not grid else len(r["buffer"]) % _FLOAT64_BYTES == 0))
@deal.ensure(lambda grid, *a, result=_DEAL_RETURN, **k: (r := _deal_return(*a, result=result)) is not None and len(r.get("column_kinds", [])) == (0 if not grid else (r["shape"][1] if len(r["shape"]) == 2 else 1)))
@deal.raises(ValueError)
def host_pack_split_grid(
    grid: list[Any] | list[list[Any]],
) -> dict[str, Any]:
    """Pack a 1D flat list or 2D mixed grid using Strategy 3: Split-Grid Serialization.

    The entire grid is flattened into a single contiguous float64 array. Integers past
    the 53-bit mantissa are not preserved. Empty cells and non-numeric strings are NaN.
    A separate sparse dictionary mapping flat cell indexes to their string value is passed in parallel.
    """
    # crosshair: off
    if not grid:
        return {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": [],
            "shape": [0],
            "strings": {},
            "buffer": b"",
        }

    buf, strings, column_kinds, shape = _flatten_grid_to_components(grid)

    envelope: dict[str, Any] = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": SPLIT_GRID_WIRE_DTYPE,
        "column_kinds": column_kinds,
        "shape": shape,
        "strings": strings,
        "buffer": buf.tobytes(),
    }

    log.debug(
        "payload_codec host_pack split_grid column_kinds=%s shape=%s cells=%s strings=%s raw_bytes=%s",
        column_kinds,
        shape,
        len(buf),
        len(strings),
        len(envelope["buffer"]),
    )

    return envelope


@deal.pre(lambda grid, *_unused, **__: _deal_product_grid_ok(grid))
# Same force/min_cells gate as should_use_binary_envelope so CrossHair cannot call pack with invalid policy kwargs.
@deal.pre(
    lambda grid, *_unused, min_cells=BINARY_MIN_CELLS, force="auto", **__: _deal_force_min_cells_ok(
        min_cells, force
    )
)
@deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None)
@deal.raises(ValueError)
def host_pack_data(
    grid: list[Any] | list[list[Any]],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> Any:
    """Pack ``data`` for worker request field (list or split_grid dict)."""
    # crosshair: off
    # First host pack loads Cython (desktop =PY(), compute HTTP host). Workers
    # use child_unpack / child_pack_split_grid and never reach this function
    # on the hot path, so they stay Inactive.
    load_cython_accelerator()
    try:
        if grid:
            if force == "always":
                return host_pack_split_grid(grid)

            nrows = len(grid)
            is_2d = type(grid[0]) in (list, tuple)

            # Row count is a cell lower bound only when the first row is non-empty.
            # 100 empty rows used to pack a split_grid with shape [100, 0].
            # Skip the max(len(r)) pass when that lower bound already qualifies.
            if is_2d and force == "auto" and nrows >= min_cells and len(grid[0]) > 0:
                return host_pack_split_grid(grid)

            # Otherwise calculate full shape for threshold check
            grid_shape: tuple[int, ...] = (
                (nrows, max((_split_grid_row_width(r) for r in grid), default=0)) if is_2d else (nrows,)
            )
            if should_use_binary_envelope(grid_shape, min_cells=min_cells, force=force):
                return host_pack_split_grid(grid)

        out = grid_from_nested_list(grid)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("payload_codec host_pack json_list %s", describe_wire_value(out))
        return out
    except Exception:
        log.exception("payload_codec host_pack failed for grid %s", describe_wire_value(grid))
        raise


@deal.pre(lambda grids, *_unused, **__: isinstance(grids, list) and len(grids) <= DEAL_MAX_SHAPE_DIM and all(_deal_product_grid_ok(g) for g in grids))
@deal.pre(
    lambda grids, *_unused, min_cells=BINARY_MIN_CELLS, force="auto", **__: _deal_force_min_cells_ok(
        min_cells, force
    )
)
@deal.post(lambda *a, result=_DEAL_RETURN, **k: _is_multi_data_envelope(_deal_return(*a, result=result)))
@deal.ensure(
    lambda grids, *a, result=_DEAL_RETURN, **k: len(_deal_return(*a, result=result).get("items", []))
    == len(grids)
)
@deal.raises(ValueError)
def host_pack_multi_data(
    grids: list[list[Any] | list[list[Any]]],
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> dict[str, Any]:
    """Pack multiple Calc ranges as a ``multi_data`` envelope for the worker."""
    # crosshair: off
    items = [host_pack_data(grid, min_cells=min_cells, force=force) for grid in grids]
    envelope: dict[str, Any] = {
        "__wa_payload__": PAYLOAD_MULTI_DATA,
        "items": items,
    }
    log.debug(
        "payload_codec host_pack multi_data items=%s cells=%s",
        len(items),
        wire_cell_count(envelope),
    )
    return envelope


def _split_grid_string_index(key: Any) -> int:
    """Flat cell index for one ``strings`` key.

    ``int(k)`` truncates a float key (``1.5`` lands in cell 1) and
    ``int(float("inf"))`` raises OverflowError. The child unpack contract
    only declares ValueError, so under deal that became RaisesContractError.
    Production keys are ``int``. Legacy harnesses used digit strings. A bool
    is an int subclass and used to become index 0 or 1; only a real ``int``,
    or one optional minus plus ASCII digits, is accepted.
    """
    # crosshair: off  # malformed wire keys (Any key, not a closed int/str domain).
    if type(key) is int:
        return key
    if type(key) is str:
        body = key[1:] if key.startswith("-") else key
        if body.isascii() and body.isdigit():
            return int(key)
    raise ValueError(f"split_grid string key {key!r} is not an integer")


def _decode_split_grid_buffer(envelope: dict[str, Any], expected_cells: int) -> bytes:
    """Return the float64 payload bytes, or raise if they do not match *expected_cells*.

    Host and child each used to pick ``buffer`` vs ``b64`` and check length.
    Decode is cold next to materializing the grid.

    The host used to slice whatever bytes arrived, so a short buffer became a
    short grid while ``wire_cell_count`` still reported the declared shape.
    The child numeric path rejected that, but the 1D mixed path reshaped only
    for 2D and returned a shorter list (or kept extra floats). Both unpackers
    call here before ``array.frombytes`` / ``np.frombuffer``, so a short,
    long, or non-multiple-of-8 buffer raises instead of changing the grid.
    """
    # crosshair: off  # buffer/b64 bytes (cover-all: envelope Any). Same reason as the unpackers.
    if "buffer" in envelope:
        raw = envelope["buffer"]
    elif "b64" in envelope:
        import base64
        import binascii

        # b64decode raises binascii.Error, and a non-ASCII string raises
        # UnicodeEncodeError. Both subclass ValueError, but deal's @deal.raises
        # matches exact types, so a bad legacy b64 became RaisesContractError
        # (AssertionError), outside _HOST_UNPACK_ERRORS. Both unpack contracts
        # declare ValueError. validate=True rejects non-alphabet characters
        # instead of skipping them. Production wire uses buffer bytes;
        # b64encode output still decodes. A non-str b64 still raises
        # AttributeError from .encode.
        try:
            raw = base64.b64decode(envelope["b64"].encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise ValueError(f"split_grid b64 is not valid base64: {exc}") from exc
    else:
        raise ValueError("Missing payload binary buffer or b64 representation")
    # Pack writes bytes. b64decode returns bytes. is_split_grid's deal pre
    # rejects a bytearray or memoryview, but a release OXT strips deal, so
    # this check is the rejection on that build too. Do not coerce them:
    # a direct legacy caller gets TypeError instead of a decoded grid.
    if not isinstance(raw, bytes):
        raise TypeError(f"a bytes-like object is required, not '{type(raw).__name__}'")
    if len(raw) % _FLOAT64_BYTES != 0:
        # Same message as array.array('d').frombytes on a truncated byte string.
        raise ValueError("bytes length not a multiple of item size")
    nvals = len(raw) // _FLOAT64_BYTES
    if nvals != expected_cells:
        raise ValueError(
            f"split_grid buffer has {nvals} values but shape {list(envelope['shape'])} needs {expected_cells}"
        )
    return raw


def _validate_split_grid_strings(envelope: dict[str, Any], expected_cells: int) -> dict[int, str]:
    raw_strings = envelope.get("strings", {})
    if not isinstance(raw_strings, dict):
        raise ValueError("split_grid strings must be a dict")
    strings: dict[int, str] = {}
    for k, v in raw_strings.items():
        ik = _split_grid_string_index(k)
        if not (0 <= ik < expected_cells):
            raise ValueError(f"split_grid string key {ik} out of bounds for {expected_cells} cells")
        if not isinstance(v, str):
            raise ValueError(
                f"split_grid string value at {ik} must be str, got {type(v).__name__}"
            )
        # 1 and "1" both int() to 1. Last-wins used to drop a cell. Egress
        # wire_str_key refuses the same collision.
        if ik in strings:
            raise ValueError(
                f"split_grid string keys collide at index {ik} ({k!r} and an earlier key)"
            )
        # np.str_ is a str subclass. Host pickle has no NumPy, so store a plain str.
        strings[ik] = v if type(v) is str else str(v)
    return strings


def _restore_cell(val: float, kind: str) -> Any:
    """One split_grid cell from its column kind.

    NaN stays NaN. Bool matches the uniform path: only exact 1.0 is True
    (a raw 2.0 used to stay a float on the mixed branch). ``int(inf)`` raises
    OverflowError, same as the uniform int path.
    """
    # crosshair: off  # mixed-cell restore; host_unpack_split_grid is already off.
    if math.isnan(val):
        return val
    if kind == "bool":
        return val == 1.0
    if kind == "int":
        return int(val)
    return val


@deal.pre(lambda envelope, *_unused, **__: _is_split_grid_envelope(envelope))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), list))
@deal.raises(ValueError, OverflowError)
def host_unpack_split_grid(envelope: dict[str, Any], *, as_nested_list: bool = True) -> list[Any] | list[list[Any]]:
    """Decode split_grid envelope on host (stdlib only). Reconstructs list or list of lists.

    NaN values in the buffer are preserved as float('nan') (they become Calc errors on =PY() egress).
    Python None is only introduced for string cells (from the strings map) or for genuine None in mixed results.
    """
    # crosshair: off
    shape = envelope["shape"]
    is_1d = len(shape) == 1
    nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])
    expected_cells = int(nrows) * int(ncols)
    buf = array.array("d")
    buf.frombytes(_decode_split_grid_buffer(envelope, expected_cells))

    # Convert keys of strings to integers in case legacy test harnesses sent stringified keys.
    # Production wire is length-prefixed Pickle5 carrying split_grid (or nested lists for < BINARY_MIN_CELLS).
    strings = _validate_split_grid_strings(envelope, expected_cells)
    # Uniform kind is only the empty-strings fast path. Building it for a wide
    # mixed grid walked every column and threw the list away. Same deferral as
    # child_unpack_split_grid.
    uniform = None if strings else envelope_uniform_column_kind(envelope, ncols=ncols)

    flat_list: list[Any]
    if not strings and uniform is not None:
        if uniform == "int":
            # Preserve NaN (as float('nan')) for int-declared columns so it surfaces as Calc error.
            # Only coerce non-NaN values to int. int(inf) raises OverflowError
            # (corrupt int column); that exception is on @deal.raises.
            flat_list = [int(v) if not math.isnan(v) else float("nan") for v in buf]
        elif uniform == "bool":
            flat_list = [(v == 1.0) if not math.isnan(v) else float("nan") for v in buf]
        else:
            # Float column: pass NaN through as float('nan') (becomes Calc error on egress).
            # Python None only comes from the strings map for genuine text/None cells.
            flat_list = list(buf)
    else:
        column_kinds = envelope_column_kinds(envelope, ncols=ncols)
        # Index the kind in the cell loop. A col_kind list of length len(buf)
        # was one Python object per cell and then thrown away.
        flat_list = [
            strings[i]
            if i in strings
            else _restore_cell(val, column_kinds[0 if is_1d else i % ncols])
            for i, val in enumerate(buf)
        ]

    if not as_nested_list or is_1d:
        return flat_list

    return [flat_list[r * ncols : (r + 1) * ncols] for r in range(nrows)]


def _deal_host_unpack_wire_ok_pytest(wire: object) -> bool:
    """Any worker result is in domain.

    datetime, Decimal, bytes, and other ordinary values failed the type list.
    PreContractError (an AssertionError) fired on the recursive call inside
    an accepted dict, before ``return wire``. The pre enumerated JSON scalars
    plus numpy. The body already returns unrecognized objects unchanged.
    CrossHair keeps that type list. ``wire`` is unused.
    """
    return True


def _deal_host_unpack_wire_ok_crosshair(wire: object) -> bool:
    return (
        _is_any_payload_envelope(wire)
        or isinstance(wire, (list, tuple, dict, str, int, float, bool))
        or wire is None
        or _is_ndarray(wire)
        or getattr(type(wire), "__module__", "") == "numpy"
    )


_deal_host_unpack_wire_ok = _profile(
    _deal_host_unpack_wire_ok_crosshair, _deal_host_unpack_wire_ok_pytest
)


@deal.pre(lambda wire, *_unused, **__: _deal_host_unpack_wire_ok(wire))
@deal.raises(*_HOST_UNPACK_ERRORS)
def host_unpack_data(wire: Any, *, as_nested_list: bool = True, _depth: int = 0) -> Any:
    """Unpack worker ``data`` or ``result`` on host (list, scalar, split_grid, multi_data, image, dataframe, calc_range)."""
    # crosshair: off
    if _depth > _MAX_UNPACK_DEPTH:
        raise ValueError("payload_codec: host_unpack_data maximum recursion depth exceeded")
    if is_image_payload(wire):
        return wire
    if is_calc_range_payload(wire):
        # Rebuilding only __wa_payload__/shape/data/address drops any other
        # envelope field. The dataframe branch already shallow-copies for the
        # same reason. Copy the dict, then replace data so a future field
        # survives. data is still unpacked.
        unpacked_inner = host_unpack_data(
            wire.get("data"), as_nested_list=as_nested_list, _depth=_depth + 1
        )
        out = dict(wire)
        out["data"] = unpacked_inner
        return out
    if is_multi_data(wire):
        items = wire.get("items") or []
        return [host_unpack_data(item, as_nested_list=as_nested_list, _depth=_depth + 1) for item in items]
    if is_split_grid(wire):
        return host_unpack_split_grid(wire, as_nested_list=as_nested_list)
    if is_dataframe_payload(wire):
        # Rebuilding only __wa_payload__/columns/data drops any other envelope
        # field (index, dtypes). The producer sets only those three today.
        # Shallow-copy and replace data so a future field survives. data is
        # still unpacked.
        unpacked_inner = host_unpack_data(
            wire.get("data"), as_nested_list=as_nested_list, _depth=_depth + 1
        )
        out = dict(wire)
        out["data"] = unpacked_inner
        return out
    # Plain dict only: CrossHair AttrDict is isinstance(dict) but blows up on __ch_pytype__ when iterating.
    # OrderedDict and other mappings come back as-is, nested envelopes still packed.
    # child_pack_result rebuilds plain dicts, so production does not hit this.
    if type(wire) is dict:
        return {k: host_unpack_data(v, as_nested_list=as_nested_list, _depth=_depth + 1) for k, v in wire.items()}
    if isinstance(wire, (list, tuple)):
        unpacked = [host_unpack_data(v, as_nested_list=as_nested_list, _depth=_depth + 1) for v in wire]
        # Tuple subclasses (namedtuple) collapse to a plain tuple. type(obj)(items)
        # TypeErrors when __new__ does not take one iterable, and a script-local
        # namedtuple is not importable on the host.
        return tuple(unpacked) if isinstance(wire, tuple) else type(wire)(unpacked)
    return wire


@deal.pre(lambda obj: _deal_wire_dict_ok(obj))
@deal.post(lambda result: isinstance(result, bool))
@inverse_ensure(
    lambda obj, result: not result
    or (
        isinstance(obj, dict)
        and obj.get("__wa_payload__") == PAYLOAD_SPLIT_GRID
        and isinstance(obj.get("shape"), list)
        and len(obj["shape"]) in (1, 2)
        and all(_nonneg_shape_dim(d) for d in obj["shape"])
        and (isinstance(obj.get("buffer"), bytes) or isinstance(obj.get("b64"), str))
    )
)
def is_split_grid(obj: Any) -> bool:
    # crosshair: off  # combinatoric Any/envelope detector (cover-all 33418536119: payload_codec 11581s after PR 523). Doable later with a closed envelope alphabet.
    return _is_split_grid_envelope(obj)


@deal.pre(lambda envelope: _is_split_grid_envelope(envelope))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None)
@deal.ensure(lambda envelope, *a, result=_DEAL_RETURN, **k: not envelope.get("strings") or isinstance(_deal_return(*a, result=result), list))
@deal.raises(ValueError, TypeError, AttributeError)
def child_unpack_split_grid(envelope: dict[str, Any]) -> Any:
    """Decode split_grid envelope in child. Returns ndarray if purely numeric, else nested lists/lists."""
    # crosshair: off
    try:
        shape = envelope["shape"]
        is_1d = len(shape) == 1
        nrows, ncols = (shape[0], 1) if is_1d else (shape[0], shape[1])

        import numpy as np

        expected_cells = int(nrows) * int(ncols)
        raw = _decode_split_grid_buffer(envelope, expected_cells)
        strings = _validate_split_grid_strings(envelope, expected_cells)
        column_kinds = envelope_column_kinds(envelope, ncols=ncols)

        if not strings:
            arr = np.frombuffer(raw, dtype=np.float64)
            if not is_1d:
                arr = arr.reshape((nrows, ncols))
            # Uniform kind only matters on this numeric path. Computing it
            # up front re-read column_kinds and threw the result away when
            # strings were present.
            arr = _apply_column_kinds_to_ndarray(
                arr,
                uniform=_uniform_column_kind(column_kinds),
            )
            log.debug("payload_codec child_unpack split_grid optimized -> ndarray shape=%s dtype=%s", arr.shape, arr.dtype)
            # Pure-numeric fast path: return ndarray directly (frombuffer + reshape + column casts).
            # This is the C-speed materialization contract for split_grid with no strings.
            # Uniform float and mixed-kind columns return that frombuffer view, which is
            # read-only. astype (int) and == (bool) already allocate a writable array.
            # Do not .copy() here; CalcRange.to_numpy() is the writable copy for =PY().
            # Callers that need Python lists (e.g. host egress) do their own conversion.
            # Mixed grids (strings present) go through the tolist + _to_py path below.
            return arr



        # Path for mixed-type grids with strings: Vectorized Object-Masking Strategy.
        #
        # --- Why this Vectorized Object-Masking Strategy? ---
        # Homogeneous numeric arrays (float64) cannot natively store Python 'None' or
        # string types. Converting the array to an 'object' type array at C-speed
        # (arr.astype(object)) allows holding arbitrary Python types. We then use
        # vectorized boolean masks to perform C-level bulk modifications, bypassing
        # slow cell-by-cell loops, modulo operations, and manual type-coercion in Python.
        arr = np.frombuffer(raw, dtype=np.float64)
        if not is_1d:
            arr = arr.reshape((nrows, ncols))

        # 1. Bulk-replace NaN values with None using a C-level boolean mask
        nan_mask = np.isnan(arr)
        obj_arr = arr.astype(object)
        obj_arr[nan_mask] = None

        # 2. Vectorized Column-Wise Casting
        # Rather than checking index-level column types inside the main cell iteration
        # (which requires modulo index maths 'i % ncols'), we iterate once per column.
        # We then cast only the valid (non-None) elements in that column at C-speed.
        has_int_or_bool = any(k in ("int", "bool") for k in column_kinds)
        if has_int_or_bool:
            col_is_int = [k == "int" for k in column_kinds]
            col_is_bool = [k == "bool" for k in column_kinds]
            for c, (is_int, is_bool) in enumerate(zip(col_is_int, col_is_bool)):
                col_slice = obj_arr[:, c] if not is_1d else obj_arr
                col_nan_mask = nan_mask[:, c] if not is_1d else nan_mask
                valid_mask = ~col_nan_mask
                if is_int:
                    # Vectorized astype(int) casts valid float objects to Python ints in C.
                    # Same corrupt-inf note as the uniform int astype: host raises
                    # OverflowError, this emits a garbage int. Pack never writes it.
                    col_slice[valid_mask] = col_slice[valid_mask].astype(int)
                elif is_bool:
                    # Host unpack: True only for 1.0. astype(bool) treated 2.0 as True.
                    numeric = np.asarray(col_slice[valid_mask], dtype=np.float64)
                    col_slice[valid_mask] = numeric == 1.0

        # 3. Sparse Strings Overlay
        # The 'strings' dictionary is sparse and indexes values row-major (flat 1D).
        # We get a flat 1D view of the object array (zero-copy ravel) to execute
        # the direct, low-overhead string insertions without coordinate math.
        if strings:
            flat_obj = obj_arr.ravel()
            for idx, val in strings.items():
                flat_obj[idx] = val


        # Convert object array to nested Python lists with native scalars
        list_result = obj_arr.tolist()
        # _to_py moved to module level
        return _to_py(list_result)
    except Exception:
        log.exception("payload_codec child_unpack split_grid failed for envelope %s", describe_wire_value(envelope))
        raise


@deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None)
@deal.raises(ValueError, TypeError, AttributeError)
def _child_unpack_single_data(wire: Any) -> Any:
    """Materialize one range payload in the venv (split_grid or nested list)."""
    # crosshair: off
    np = _optional_numpy()

    unpacked = child_unpack_split_grid(wire) if is_split_grid(wire) else wire

    # Single-cell ranges become scalars; multi-range outer list is handled by child_unpack_data.
    if np is not None and isinstance(unpacked, np.ndarray):
        if unpacked.size == 1:
            # Keep 1.0 as float. int(val) made a 1×1 cell an int while a longer
            # float64 column stayed 1.0.
            # Asymmetry, left as-is: this size==1 ndarray (a length-1 vector, or
            # force="always" on [[x]]) becomes a scalar. A nested list [[x]] is
            # not an ndarray yet and stays 2D
            # (test_child_unpack_single_entry_auto_scalar_and_integer_coercion).
            # force="auto" never binary-packs one cell, so production single
            # cells arrive as lists. Do not collapse [[x]] to match this path.
            return unpacked.item()
    elif isinstance(unpacked, (list, tuple)):
        if len(unpacked) == 1 and type(unpacked[0]) not in (list, tuple):
            return unpacked[0]

        grid: list[Any] | list[list[Any]]
        if unpacked and (type(unpacked[0]) in (list, tuple)):
            # list(row) on a later int raises TypeError. This function has no
            # grid pre, so deal did not hide it. Host pack already raises
            # ValueError via _split_grid_row_width. A real row is a list or
            # tuple. A str has a length but is not a row ("ab" must not become
            # two cells).
            grid = []
            for row in unpacked:
                _split_grid_row_width(row)
                grid.append(list(row))
        else:
            grid = list(unpacked)
        if is_numeric_grid(grid):
            # is_numeric_coercible treats whitespace/"" as Calc blanks, but
            # np.float64 cannot convert those strings (ValueError). Keep the list.
            # No NumPy: a numeric list cannot become an ndarray. Return it so
            # plain inbound data still materializes.
            if np is None:
                log.debug(
                    "payload_codec child_unpack json_list as-is (numpy unavailable) %s",
                    describe_wire_value(unpacked),
                )
                return grid
            try:
                arr = np.array(grid, dtype=np.float64)
            except ValueError:
                log.debug(
                    "payload_codec child_unpack json_list as-is (non-floatable blanks) %s",
                    describe_wire_value(unpacked),
                )
                return grid
            log.debug(
                "payload_codec child_unpack json_list -> ndarray shape=%s",
                arr.shape,
            )
            return arr
        if log.isEnabledFor(logging.DEBUG):
            log.debug("payload_codec child_unpack json_list as-is %s", describe_wire_value(unpacked))
        return grid
    return unpacked


def _deal_child_unpack_wire_ok_pytest(wire: object) -> bool:
    """Child wire ingest is total on the pytest profile.

    Same class of bug as ``host_unpack_data``: an odd but real payload
    raised PreContractError instead of the body's ValueError/passthrough.
    CrossHair keeps the closed type list. ``wire`` is unused.
    """
    return True


def _deal_child_unpack_wire_ok_crosshair(wire: object) -> bool:
    return (
        _is_any_payload_envelope(wire)
        or isinstance(wire, (list, tuple, dict, str, int, float, bool))
        or wire is None
        or (hasattr(wire, "__class__") and wire.__class__.__name__ == "ndarray")
    )


_deal_child_unpack_wire_ok = _profile(
    _deal_child_unpack_wire_ok_crosshair, _deal_child_unpack_wire_ok_pytest
)


@deal.pre(lambda wire, *_unused, **__: _deal_child_unpack_wire_ok(wire))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result) is not None)
@deal.raises(ValueError, TypeError, AttributeError)
def child_unpack_data(wire: Any, *, _depth: int = 0) -> Any:
    """Materialize worker ``data`` in venv.

    For ``calc_range`` / ``multi_data`` of ranges, returns a :class:`CalcRange` or
    list of CalcRange (caller should prefer :func:`materialize_inputs`).
    Legacy bare grids still become ndarray/list.
    """
    # crosshair: off
    if _depth > _MAX_UNPACK_DEPTH:
        raise ValueError("payload_codec: child_unpack_data maximum recursion depth exceeded")
    try:
        from plugin.scripting.calc_range import materialize_calc_range

        if is_calc_range_payload(wire):
            return materialize_calc_range(wire)
        if is_multi_data(wire):
            items = wire.get("items") or []
            return [child_unpack_data(item, _depth=_depth + 1) for item in items]
        if is_split_grid(wire):
            return _child_unpack_single_data(wire)
        if is_image_payload(wire):
            return wire
        # Host walks plain dicts. This used to return them unchanged, so a
        # split_grid nested under a dict reached user code still packed.
        # Lists stay as-is: the list arm is the grid materializer, and turning
        # a numeric list stored in a dict into an ndarray would change
        # test_child_unpack_plain_python_without_numpy. Only dict values can
        # be envelopes. OrderedDict is not ``type is dict`` (same AttrDict
        # rule as host_unpack_data).
        if type(wire) is dict:
            return {
                k: child_unpack_data(v, _depth=_depth + 1) if type(v) is dict else v
                for k, v in wire.items()
            }
        return _child_unpack_single_data(wire)
    except Exception:
        log.exception(
            "payload_codec child_unpack failed for wire %s",
            describe_wire_value(wire),
        )
        raise


def _is_numeric_wire_kind(kind: str | None) -> bool:
    """True when ``astype(float64)`` on split_grid keeps the value.

    Integer, unsigned, float, and bool only. Complex (``c``) would drop the
    imaginary part with a ComplexWarning. Void/structured (``V``) raises inside
    the cast. datetime64 (``M``) and timedelta64 (``m``) become Unix-epoch
    units, not Calc serials or ISO text — ``_reject_temporal_ndarray`` still
    raises for those before ``tolist()``, which is integer nanoseconds.
    """
    # crosshair: off
    return kind in ("i", "u", "f", "b")


def _reject_temporal_ndarray(arr: Any) -> None:
    """Refuse datetime64/timedelta64 on the float64 pack path.

    ``astype(float64)`` turns a date into a Unix-epoch day count (20629.0),
    and ``.tolist()`` on datetime64[ns] yields integer nanoseconds.
    ``child_pack_result`` used to send kind ``M``/``m`` down the numeric lane.
    ``serialize_result`` converts those arrays before it calls here; a direct
    caller gets ``ValueError`` instead of a silent wrong number.
    """
    # crosshair: off
    kind = getattr(getattr(arr, "dtype", None), "kind", None)
    if kind in ("M", "m"):
        raise ValueError(
            "datetime64/timedelta64 cannot be packed as float64 "
            "(astype would emit Unix-epoch units, and tolist() yields integer "
            "nanoseconds). serialize_result converts these before packing."
        )


def wire_str_key(key: Any, used: set[str]) -> str:
    """Stringify a dict key for the host pickle boundary.

    ``{str(k): ...}`` is last-wins, so ``{1: "a", "1": "b"}`` dropped ``"a"``
    with no error. Three egress sites each rebuilt the dict that way. The
    second key that stringifies to an existing wire key raises before the
    value is overwritten. An exact ``str`` is kept; a subclass such as
    ``np.str_`` becomes a builtin ``str``.
    """
    # crosshair: off
    sk = key if type(key) is str else str(key)
    if sk in used:
        raise ValueError(
            f"dict keys collide when stringified to {sk!r}; refusing to drop a value"
        )
    used.add(sk)
    return sk


@deal.pre(lambda arr: _is_ndarray(arr))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), dict))
@deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("__wa_payload__") == PAYLOAD_SPLIT_GRID)
@deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("dtype") == SPLIT_GRID_WIRE_DTYPE)
@deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result).get("buffer"), bytes))
@deal.ensure(lambda arr, *a, result=_DEAL_RETURN, **k: _deal_return(*a, result=result).get("strings") == {})
@deal.raises(ValueError, TypeError, AttributeError)
def child_pack_split_grid(arr: Any) -> dict[str, Any]:
    """Pack ndarray as split_grid for JSON wire (venv). Numeric lane is always float64 bytes.

    datetime64/timedelta64 must not be passed here — ``astype(float64)`` is Unix-epoch
    units, not Calc serials. ``serialize_result`` converts those to ISO / timedelta first.
    """
    # crosshair: off
    import numpy as np

    try:
        if not isinstance(arr, np.ndarray):
            arr = np.asarray(arr)
        _reject_temporal_ndarray(arr)
        # Rank 3+ used to be written as shape=[d0,d1,d2]. The detector only
        # accepts rank 1 or 2, so host_unpack treated the envelope as a plain
        # metadata dict (raw bytes included). A 0-d array did the same with
        # shape=[].
        if arr.ndim not in (1, 2):
            raise ValueError(f"split_grid supports rank 1 or 2, got ndim {arr.ndim}")
        kind = getattr(getattr(arr, "dtype", None), "kind", None)
        if not _is_numeric_wire_kind(kind if isinstance(kind, str) else None):
            raise ValueError(
                f"split_grid float64 lane accepts dtype kinds i/u/f/b, got {kind!r}"
            )
        ncols = int(arr.shape[1]) if arr.ndim == 2 else 1
        # A bool ndarray is not an integer dtype. Tagging it "float" made host
        # unpack restore 1.0/0.0 instead of True/False.
        if np.issubdtype(arr.dtype, np.bool_):
            column_kinds = ["bool"] * ncols
        elif np.issubdtype(arr.dtype, np.integer):
            column_kinds = ["int"] * ncols
        else:
            column_kinds = ["float"] * ncols
        wire_arr = np.ascontiguousarray(arr, dtype=np.float64)
        envelope: dict[str, Any] = {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "dtype": SPLIT_GRID_WIRE_DTYPE,
            "column_kinds": column_kinds,
            "shape": list(wire_arr.shape),
            "strings": {},
            "buffer": wire_arr.tobytes(),
        }
        log.debug(
            "payload_codec child_pack split_grid column_kinds=%s shape=%s cells=%s raw_bytes=%s",
            column_kinds,
            wire_arr.shape,
            wire_arr.size,
            len(envelope["buffer"]),
        )
        return envelope
    except Exception:
        log.exception(
            "payload_codec child_pack split_grid failed for value %s",
            describe_wire_value(arr),
        )
        raise


def _container_has_packable_nested(obj: Any, _depth: int = 0, *, _np: Any) -> bool:
    """True when *obj* contains ndarray/dict containers that need per-element packing.

    ``_np`` is resolved once by the caller. Recursion must not call
    ``_optional_numpy()`` again. A module-level cache would also hide the
    ``sys.modules['numpy'] = None`` tests.
    """
    # crosshair: off  # recursive Any (cover-all 33355986432: payload_codec in-flight 6h with sandbox_cache, no flushed COVER TIMING). Doable later with _deal_envelope_value_ok.
    if _depth > _MAX_UNPACK_DEPTH:
        raise ValueError("payload_codec: maximum recursion depth exceeded")
    containers = (dict, _np.ndarray) if _np is not None else (dict,)

    if isinstance(obj, containers):
        return True
    if isinstance(obj, (list, tuple)):
        for item in obj:
            if isinstance(item, containers):
                return True
            if isinstance(item, (list, tuple)) and _container_has_packable_nested(
                item, _depth=_depth + 1, _np=_np
            ):
                return True
    return False


def _needs_elementwise_pack(obj: Any, _depth: int = 0) -> bool:
    """True when a list/tuple should be packed element-wise instead of as one grid."""
    # crosshair: off  # recursive Any (cover-all 33355986432: payload_codec in-flight 6h with sandbox_cache, no flushed COVER TIMING). Doable later with _deal_envelope_value_ok.
    if _depth > _MAX_UNPACK_DEPTH:
        raise ValueError("payload_codec: maximum recursion depth exceeded")
    # A top-level ndarray is one grid (the caller packs it). A dict is not.
    if isinstance(obj, dict):
        return True
    if not isinstance(obj, (list, tuple)) or not obj:
        return False
    np = _optional_numpy()
    return any(_container_has_packable_nested(item, _depth=_depth + 1, _np=np) for item in obj)


@deal.pre(lambda result, *_unused, **__: True)
@deal.post(lambda _unused: True)
@deal.raises(ValueError, TypeError, AttributeError)
def child_pack_result(
    result: Any,
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
    _depth: int = 0,
) -> Any:
    """Pack a worker result. Not pickle-safe for every small-list cell.

    Top-level NumPy scalars become builtin int/float/bool. An ndarray becomes a
    list or a split_grid envelope.     Inside a small list, ``_cell_for_json`` only
    rewrites ``None``; ``np.int64``, ``Decimal``, and ``Fraction`` pass through.
    ``serialize_result`` must run ``_coerce_host_pickle_tree`` on those
    containers, on object-ndarray ``tolist()`` output, and on
    datetime64/timedelta64 arrays first. ``float('nan')`` stays NaN.
    """
    # crosshair: off
    # pre/post are intentionally ``lambda: True``. serialization-verification.md:
    # dispatch wrappers keep a minimal contract; branch guarantees live in pytest.
    if _depth > _MAX_UNPACK_DEPTH:
        raise ValueError("payload_codec: child_pack_result maximum recursion depth exceeded")
    np = _optional_numpy()

    try:
        if np is not None:
            if isinstance(result, np.ndarray):
                shape = tuple(int(x) for x in result.shape)
                kind = getattr(result.dtype, "kind", None)
                # Before either the split_grid cast or tolist(). Both rewrite dates.
                _reject_temporal_ndarray(result)
                if result.ndim > 2:
                    # A rank-3 tolist() looks like a rectangular 2D grid whose
                    # cells are rows. The list packer would then split_grid that
                    # outer plane and stringify the inner rows. Calc spill is
                    # 2D, so pack one axis at a time. A first-class ND envelope
                    # would need the detector and both unpackers together — do
                    # not write a rank-3 shape into this envelope.
                    return [
                        child_pack_result(
                            result[i], min_cells=min_cells, force=force, _depth=_depth + 1
                        )
                        for i in range(int(result.shape[0]))
                    ]
                # The old gate was ``kind not in ("U", "S", "O")``. Complex
                # (``c``) passed it and astype(float64) dropped the imaginary
                # part. Void/structured raised inside the cast and dropped the
                # cell. Same whitelist as the DataFrame path.
                wire_kind = kind if isinstance(kind, str) else None
                if (
                    result.ndim in (1, 2)
                    and _is_numeric_wire_kind(wire_kind)
                    and should_use_binary_envelope(shape, min_cells=min_cells, force=force)
                ):
                    return child_pack_split_grid(result)
                # Logging json_list egress and then returning the ndarray
                # unchanged left a DataFrame under 100 cells with an ndarray
                # body, and result_to_calc_grid dropped that body. A bare
                # multi-cell array became one Calc string. Recurse on tolist()
                # so the list path (grid_from_nested_list) is what goes on the wire.
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "payload_codec child_pack ndarray via list kind=%s shape=%s",
                        kind,
                        shape,
                    )
                return child_pack_result(result.tolist(), min_cells=min_cells, force=force, _depth=_depth + 1)
            elif isinstance(result, np.integer):
                return int(result)
            elif isinstance(result, np.floating):
                return float(result)
            elif isinstance(result, np.bool_):
                return bool(result)
        if isinstance(result, dict):
            used: set[str] = set()
            packed_dict: dict[str, Any] = {}
            for key, value in result.items():
                sk = wire_str_key(key, used)
                packed_dict[sk] = child_pack_result(
                    value, min_cells=min_cells, force=force, _depth=_depth + 1
                )
            return packed_dict
        if isinstance(result, (list, tuple)):
            if _needs_elementwise_pack(result, _depth=_depth+1):
                packed = [child_pack_result(x, min_cells=min_cells, force=force, _depth=_depth + 1) for x in result]
                # Tuple subclasses (namedtuple) collapse to a plain tuple. type(obj)(items)
                # TypeErrors when __new__ does not take one iterable, and a script-local
                # namedtuple is not importable on the host.
                return tuple(packed) if isinstance(result, tuple) else type(result)(packed)
            if (
                result
                and type(result[0]) in (list, tuple)
                and all(
                    isinstance(r, (list, tuple))
                    and len(r) == len(result[0])
                    and all(not isinstance(cell, (list, tuple)) for cell in r)
                    for r in result
                )
            ):
                # Strict rectangular 2D grid of scalar cells. A list-of-grids
                # ([[[i], [i]], ...]) is rectangular in the outer shape, but
                # each cell is a row. split_grid then stored str(cell)
                # ("[0]") once rows*cols >= BINARY_MIN_CELLS. The jagged
                # branch below packs each row on its own, same as rank-3
                # ndarrays. str and bytes are not list/tuple, so they stay cells.
                # Tuple rows become lists. Split_grid is a float64 buffer and
                # cannot keep row container types, so the list path matches it.
                # host_unpack_data restores a tuple only when the wire value is
                # still a tuple; this branch does not send tuples.
                grid = [list(row) for row in result]
                grid_shape: tuple[int, ...] = (len(grid), max((len(r) for r in grid), default=0))
            elif result and type(result[0]) in (list, tuple):
                # Jagged: first item is a row but later items are not (e.g. [[None], None]).
                packed = [child_pack_result(x, min_cells=min_cells, force=force, _depth=_depth + 1) for x in result]
                # Same tuple-subclass collapse as the elementwise branch above.
                return tuple(packed) if isinstance(result, tuple) else type(result)(packed)
            else:
                grid = list(result)
                grid_shape = (len(grid),)
            if should_use_binary_envelope(grid_shape, min_cells=min_cells, force=force):
                # Shared stdlib grid packer (also used by host_pack_data). The
                # host_ name is historical; this path runs in the venv too.
                return host_pack_split_grid(grid)
            out = grid_from_nested_list(grid)
            if log.isEnabledFor(logging.DEBUG):
                log.debug("payload_codec child_pack json_list egress %s", describe_wire_value(out))
            return out
        return result
    except Exception:
        log.exception(
            "payload_codec child_pack_result failed for value %s",
            describe_wire_value(result),
        )
        raise



