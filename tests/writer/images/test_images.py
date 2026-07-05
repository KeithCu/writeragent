# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for images.py helpers (no LibreOffice required)."""
from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.images.images import _resolve_orient


def test_resolve_orient_named_positions():
    # Friendly names resolve to a (mocked) UNO constant, never an error.
    for name in ("left", "center", "right", "LEFT", "Right"):
        const, err = _resolve_orient(name, "hori")
        assert err is None and const is not None
    for name in ("top", "center", "bottom"):
        const, err = _resolve_orient(name, "vert")
        assert err is None and const is not None


def test_resolve_orient_centre_british_spelling():
    const, err = _resolve_orient("centre", "hori")
    assert err is None and const is not None


def test_resolve_orient_int_passthrough():
    # Raw UNO integer constants are accepted unchanged (back-compat).
    assert _resolve_orient(3, "hori") == (3, None)
    assert _resolve_orient(0, "vert") == (0, None)


def test_resolve_orient_unknown_name_errors():
    const, err = _resolve_orient("middle", "hori")
    assert const is None
    assert err and "middle" in err and "left" in err  # error lists valid options


def test_resolve_orient_bool_rejected():
    # bool is an int subclass — must not be silently treated as an orientation constant.
    const, err = _resolve_orient(True, "hori")
    assert const is None and err is not None


def test_resolve_orient_vert_rejects_hori_name():
    # 'left' is not a vertical position.
    const, err = _resolve_orient("left", "vert")
    assert const is None and err is not None and "top" in err
