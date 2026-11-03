from __future__ import annotations
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ascii_bounded helper and deal_shim constants."""


import ast
import os
import subprocess
import sys
import textwrap

import pytest

from plugin.framework.deal_shim import (
    DEAL_MAX_ARGV,
    DEAL_MAX_BACKOFF,
    DEAL_MAX_BACKOFF_FACTOR,
    DEAL_MAX_CELL_REF,
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_COL_INDEX,
    DEAL_MAX_COL_LETTERS,
    DEAL_MAX_HTML_CHUNK,
    DEAL_MAX_MSGID,
    DEAL_MAX_ORIGIN,
    DEAL_MAX_PATH,
    DEAL_MAX_PLACEHOLDER_INDEX,
    DEAL_MAX_RETRY,
    DEAL_MAX_ROW_INDEX,
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SHAPE_RANK,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    DEAL_MAX_URL,
    DEAL_MAX_XL_EXPR,
    _noop_contract,
    ascii_bounded,
    deal,
    inverse_ensure,
    str_bounded,
)


def test_ascii_bounded_valid_ascii() -> None:
    assert ascii_bounded("A1", 8) is True
    assert ascii_bounded("A0", 8) is True
    assert ascii_bounded("", 8) is True
    assert ascii_bounded("", 8, min_len=1) is False


def test_ascii_bounded_unicode_rejected() -> None:
    assert ascii_bounded("A🯰", 8) is False
    assert ascii_bounded("é", 8) is False


def test_ascii_bounded_length_limits() -> None:
    # Pytest/debug CELL_REF is 256 (sheet-qualified live ranges); not the old 32.
    assert ascii_bounded("A" * DEAL_MAX_CELL_REF, DEAL_MAX_CELL_REF) is True
    assert ascii_bounded("A" * (DEAL_MAX_CELL_REF + 1), DEAL_MAX_CELL_REF) is False
    assert ascii_bounded("abc", 5, min_len=4) is False
    assert ascii_bounded("abcd", 5, min_len=4) is True


def test_ascii_bounded_non_string_types() -> None:
    assert ascii_bounded(None, 8) is False
    assert ascii_bounded(123, 8) is False
    assert ascii_bounded(["A1"], 8) is False


def test_str_bounded_allows_unicode() -> None:
    assert str_bounded("✓ Copied!", 64) is True
    assert str_bounded("Testing…", 64) is True
    assert str_bounded("A🯰", 8) is True
    assert str_bounded("", 8) is True
    assert str_bounded("é" * 9, 8) is False
    assert str_bounded(None, 8) is False
    assert str_bounded(123, 8) is False


def test_deal_shim_constants_match_pytest_profile() -> None:
    """Unset env / pytest binds the wide product-faithful table."""
    assert DEAL_MAX_COL_LETTERS == 3
    assert DEAL_MAX_COL_INDEX == 26 + 26**2 + 26**3 - 1 == 18277
    assert DEAL_MAX_CELL_REF == 256
    assert DEAL_MAX_TOKEN == 64
    assert DEAL_MAX_XL_EXPR == 64
    assert DEAL_MAX_ORIGIN == 256
    assert DEAL_MAX_URL == 256
    assert DEAL_MAX_PATH == 256
    assert DEAL_MAX_ARGV == 4096
    assert DEAL_MAX_CMD_ARGS == 32
    assert DEAL_MAX_SOURCE == 8192
    assert DEAL_MAX_MSGID == 1024
    assert DEAL_MAX_ROW_INDEX == 1_048_575
    assert DEAL_MAX_PLACEHOLDER_INDEX == 64
    assert DEAL_MAX_SHAPE_RANK == 4
    assert DEAL_MAX_SHAPE_DIM == 256
    assert DEAL_MAX_RETRY == 8
    assert DEAL_MAX_BACKOFF == 300.0
    assert DEAL_MAX_BACKOFF_FACTOR == 10.0
    assert DEAL_MAX_HTML_CHUNK == 4096
    assert inverse_ensure is deal.ensure


def test_noop_contract() -> None:
    def f() -> str:
        return "ok"

    assert _noop_contract()(f) is f
    assert _noop_contract(lambda result: True)(f) is f


def test_crosshair_domains_runner_hook_and_parent_width() -> None:
    """Parent process retains product-faithful wide table; child with install() gets short bounds."""
    from plugin.framework.ast_stmt_edit import is_name_call_expr
    from plugin.framework.html_stripper import StreamingHTMLStripper, strip_html_tags
    from plugin.framework.i18n import _
    from plugin.mcp.cors import is_safe_origin, normalize_cors_origin
    from plugin.scripting.payload_codec import is_image_payload
    from tests.harness.strip_bundle import deal_pre_present

    # Parent process retains wide table
    assert DEAL_MAX_COL_INDEX == 18277
    assert DEAL_MAX_ROW_INDEX == 1_048_575
    assert DEAL_MAX_CELL_REF == 256

    assert is_safe_origin("http://localhost:3000") is True
    assert normalize_cors_origin("https://localai.local/") == "https://localai.local"
    assert strip_html_tags("<b>ok</b>") == "ok"
    assert _("✓ Copied!") == "✓ Copied!"
    product_origin = "http://" + ("a" * 200)
    if not deal_pre_present(is_safe_origin):
        return
    assert len(product_origin) <= DEAL_MAX_ORIGIN
    is_safe_origin(product_origin)
    # Pytest origin ingest is total: a huge Origin is unsafe, not a contract error.
    assert is_safe_origin("h" * (DEAL_MAX_ORIGIN + 1)) is False
    # feed() / strip_html_tags slice; overflow is per-slice on _feed_chunk.
    strip_html_tags("x" * (DEAL_MAX_HTML_CHUNK + 1))
    StreamingHTMLStripper().feed("x" * (DEAL_MAX_HTML_CHUNK + 1))
    with pytest.raises(deal.PreContractError):
        StreamingHTMLStripper()._feed_chunk("x" * (DEAL_MAX_HTML_CHUNK + 1))
    strip_html_tags("x" * 256)

    five_keys = {f"k{i}": i for i in range(5)}
    five_names = frozenset(f"n{i}" for i in range(5))
    node = ast.Expr(value=ast.Call(func=ast.Name(id="x", ctx=ast.Load()), args=[], keywords=[]))
    assert is_image_payload(five_keys) is False
    assert is_name_call_expr(node, five_names) is False
    assert is_image_payload({f"k{i}": i for i in range(DEAL_MAX_SHAPE_DIM + 1)}) is False
    with pytest.raises(deal.PreContractError):
        is_name_call_expr(node, frozenset(f"n{i}" for i in range(DEAL_MAX_CMD_ARGS + 1)))
    is_name_call_expr(node, frozenset({"xl"}))

    script = textwrap.dedent(
        """
        import ast
        import sys
        from unittest.mock import patch
        import deal
        from scripts.crosshair_domains import install
        install()

        from plugin.framework.deal_shim import (
            DEAL_MAX_ARGV,
            DEAL_MAX_BACKOFF,
            DEAL_MAX_BACKOFF_FACTOR,
            DEAL_MAX_CELL_REF,
            DEAL_MAX_CMD_ARGS,
            DEAL_MAX_COL_INDEX,
            DEAL_MAX_COL_LETTERS,
            DEAL_MAX_HTML_CHUNK,
            DEAL_MAX_MSGID,
            DEAL_MAX_ORIGIN,
            DEAL_MAX_PATH,
            DEAL_MAX_PLACEHOLDER_INDEX,
            DEAL_MAX_RETRY,
            DEAL_MAX_ROW_INDEX,
            DEAL_MAX_SHAPE_DIM,
            DEAL_MAX_SHAPE_RANK,
            DEAL_MAX_SOURCE,
            DEAL_MAX_TOKEN,
            DEAL_MAX_URL,
            DEAL_MAX_XL_EXPR,
            inverse_ensure,
        )
        assert DEAL_MAX_COL_LETTERS == 1, DEAL_MAX_COL_LETTERS
        assert DEAL_MAX_COL_INDEX == 25, DEAL_MAX_COL_INDEX
        assert DEAL_MAX_CELL_REF == 4, DEAL_MAX_CELL_REF
        assert DEAL_MAX_ROW_INDEX == 20, DEAL_MAX_ROW_INDEX
        assert DEAL_MAX_ARGV == 32, DEAL_MAX_ARGV
        assert DEAL_MAX_CMD_ARGS == 4, DEAL_MAX_CMD_ARGS
        assert DEAL_MAX_SHAPE_DIM == 4, DEAL_MAX_SHAPE_DIM
        assert DEAL_MAX_SHAPE_RANK == 2, DEAL_MAX_SHAPE_RANK
        assert DEAL_MAX_PLACEHOLDER_INDEX == 4, DEAL_MAX_PLACEHOLDER_INDEX
        assert DEAL_MAX_SOURCE == 16, DEAL_MAX_SOURCE
        assert DEAL_MAX_MSGID == 1024, DEAL_MAX_MSGID
        assert DEAL_MAX_PATH == 32, DEAL_MAX_PATH
        assert DEAL_MAX_TOKEN == 16, DEAL_MAX_TOKEN
        assert DEAL_MAX_XL_EXPR == 32, DEAL_MAX_XL_EXPR
        assert DEAL_MAX_ORIGIN == 32, DEAL_MAX_ORIGIN
        assert DEAL_MAX_URL == 32, DEAL_MAX_URL
        assert DEAL_MAX_RETRY == 8, DEAL_MAX_RETRY
        assert DEAL_MAX_BACKOFF == 300.0, DEAL_MAX_BACKOFF
        assert DEAL_MAX_BACKOFF_FACTOR == 10.0, DEAL_MAX_BACKOFF_FACTOR
        assert DEAL_MAX_HTML_CHUNK == 16, DEAL_MAX_HTML_CHUNK

        def _f(x):
            return x + 1
        _wrapped = inverse_ensure(lambda x, result: False)(_f)
        assert _wrapped is _f and _wrapped(3) == 4

        from plugin.calc.address_utils import (
            index_to_column,
            format_address,
            parse_address,
        )
        # address_utils 25/20/4
        assert index_to_column(25) == "Z"
        try:
            index_to_column(26)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("col index 26 must fail under CrossHair table")

        assert format_address(0, 20) == "A21"
        try:
            format_address(0, 21)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("row index 21 must fail under CrossHair table")

        parse_address("A1")
        try:
            parse_address("A" * 5)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("cell ref len 5 must fail under CrossHair table")

        from plugin.mcp.cors import is_safe_origin
        try:
            is_safe_origin("h" * 33)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("origin 33 must fail under CrossHair table")

        from plugin.framework.html_stripper import StreamingHTMLStripper
        strip_instance = StreamingHTMLStripper()
        try:
            strip_instance._feed_chunk("x" * 17)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("html _feed_chunk 17 must fail under CrossHair table")

        from plugin.framework.i18n import _
        _("x" * 1024)
        try:
            _("x" * 1025)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("msgid 1025 must still fail")

        from plugin.scripting.payload_codec import is_image_payload
        five_keys = {f"k{i}": i for i in range(5)}
        try:
            is_image_payload(five_keys)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("5-key dict must fail under CrossHair SHAPE_DIM=4")

        from plugin.framework.ast_stmt_edit import is_name_call_expr
        node = ast.Expr(value=ast.Call(func=ast.Name(id="x", ctx=ast.Load()), args=[], keywords=[]))
        five_names = frozenset(f"n{i}" for i in range(5))
        try:
            is_name_call_expr(node, five_names)
        except deal.PreContractError:
            pass
        else:
            raise SystemExit("5 names must fail under CrossHair CMD_ARGS=4")

        # Verify run_crosshair argv includes python -c with install() and sys.argv
        import io
        from scripts.crosshair_stream import StreamStats, run_crosshair
        captured_cmd = []
        def fake_popen(cmd, *a, **k):
            captured_cmd.append(cmd)
            class FakeProc:
                stdout = io.StringIO("")
                def poll(self): return 0
                def wait(self): return 0
            return FakeProc()

        with patch("subprocess.Popen", side_effect=fake_popen), patch("scripts.crosshair_stream.stream_lines", return_value=StreamStats()):
            run_crosshair("check", ["target.py"], mode="check", raw=True, quiet=True)
        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert cmd[0] == sys.executable
        assert cmd[1] == "-c"
        assert "crosshair_domains" in cmd[2]
        assert "install" in cmd[2]
        assert cmd[3:] == ["crosshair", "check", "target.py"]
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
