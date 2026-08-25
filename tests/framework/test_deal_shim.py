# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for ascii_bounded helper and deal_shim constants."""

from __future__ import annotations

import os

from plugin.framework.deal_shim import (
    CROSSHAIR_ENV,
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
    ascii_bounded,
    deal,
    deal_maxima,
    inverse_ensure,
    inverse_ensure_for,
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
    assert ascii_bounded("A" * 32, DEAL_MAX_CELL_REF) is True
    assert ascii_bounded("A" * 33, DEAL_MAX_CELL_REF) is False
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
    assert os.environ.get(CROSSHAIR_ENV) != "1"
    wide = deal_maxima(crosshair=False)
    assert DEAL_MAX_COL_LETTERS == wide.col_letters == 3
    assert DEAL_MAX_COL_INDEX == wide.col_index == 26 + 26**2 + 26**3 - 1 == 18277
    assert DEAL_MAX_CELL_REF == wide.cell_ref == 32
    assert DEAL_MAX_TOKEN == wide.token == 64
    assert DEAL_MAX_XL_EXPR == wide.xl_expr == 64
    assert DEAL_MAX_ORIGIN == wide.origin == 256
    assert DEAL_MAX_URL == wide.url == 256
    assert DEAL_MAX_PATH == wide.path == 256
    assert DEAL_MAX_ARGV == wide.argv == 4096
    assert DEAL_MAX_CMD_ARGS == wide.cmd_args == 32
    assert DEAL_MAX_SOURCE == wide.source == 8192
    assert DEAL_MAX_MSGID == wide.msgid == 1024
    assert DEAL_MAX_ROW_INDEX == wide.row_index == 1_048_575
    assert DEAL_MAX_PLACEHOLDER_INDEX == wide.placeholder_index == 64
    assert DEAL_MAX_SHAPE_RANK == wide.shape_rank == 4
    assert DEAL_MAX_SHAPE_DIM == wide.shape_dim == 256
    assert DEAL_MAX_RETRY == wide.retry == 8
    assert DEAL_MAX_BACKOFF == wide.backoff == 300.0
    assert DEAL_MAX_BACKOFF_FACTOR == wide.backoff_factor == 10.0
    assert DEAL_MAX_HTML_CHUNK == wide.html_chunk == 512
    assert inverse_ensure is deal.ensure


def test_deal_maxima_crosshair_profile_stays_tiny() -> None:
    """Short table cannot drift; pair col_letters with col_index on both sides."""
    short = deal_maxima(crosshair=True)
    assert short.col_letters == 1
    assert short.col_index == 25
    assert short.cell_ref == 4
    assert short.row_index == 20
    assert short.argv == 32
    assert short.cmd_args == 4
    assert short.shape_dim == 4
    assert short.shape_rank == 2
    assert short.placeholder_index == 4
    assert short.source == 16
    assert short.msgid == 1024
    assert short.path == 32
    assert short.token == 16
    assert short.xl_expr == 32
    assert short.html_chunk == 64
    # Unchanged unless a test-backed reason appears.
    assert short.origin == 256
    assert short.url == 256
    assert short.retry == 8
    assert short.backoff == 300.0
    assert short.backoff_factor == 10.0


def test_inverse_ensure_for_is_noop_under_crosshair() -> None:
    def f(x: int) -> int:
        return x + 1

    wrapped = inverse_ensure_for(crosshair=True)(lambda x, result: False)(f)
    assert wrapped is f
    assert wrapped(3) == 4


def test_inverse_ensure_for_is_deal_ensure_under_pytest() -> None:
    assert inverse_ensure_for(crosshair=False) is deal.ensure


def test_cover_all_and_check_all_main_set_crosshair_env(tmp_path, monkeypatch) -> None:
    """Both sweep mains set CROSSHAIR_ENV before any CrossHair spawn (--list is enough)."""
    from scripts.crosshair_check_all import main as check_all_main
    from scripts.crosshair_cover_all import main as cover_all_main

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "m.py").write_text(
        "import deal\n@deal.post(lambda result: True)\ndef f():\n    return 1\n",
        encoding="utf-8",
    )
    assert cover_all_main(["--list", "--plugin-root", str(plugin)]) == 0
    assert os.environ.get(CROSSHAIR_ENV) == "1"
    # Pop without a second monkeypatch.delenv — that would snapshot "1" as the
    # restore value and leak the env into later pytest cases.
    os.environ.pop(CROSSHAIR_ENV, None)
    assert check_all_main(["--list", "--plugin-root", str(plugin)]) == 0
    assert os.environ.get(CROSSHAIR_ENV) == "1"


def test_cover_all_workers_bind_short_deal_table(monkeypatch) -> None:
    """Cover-all pool workers must see deal_maxima(crosshair=True), not pytest's wide table.

    Spawn (not fork): pytest has already imported deal_shim with the wide bind;
    fork would copy that. Cover-all's initializer + worker_deal_maxima set
    WRITERAGENT_CROSSHAIR=1 before the worker imports deal_shim.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from scripts.crosshair_cover_all import enable_crosshair_deal_table, worker_deal_maxima
    from scripts.crosshair_stream import CROSSHAIR_DEAL_ENV

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)
    assert CROSSHAIR_DEAL_ENV == CROSSHAIR_ENV == "WRITERAGENT_CROSSHAIR"
    assert os.environ.get(CROSSHAIR_ENV) != "1"
    wide = deal_maxima(crosshair=False)
    short = deal_maxima(crosshair=True)
    assert DEAL_MAX_COL_INDEX == wide.col_index == 18277
    assert DEAL_MAX_ROW_INDEX == wide.row_index == 1_048_575
    assert DEAL_MAX_CELL_REF == wide.cell_ref == 32
    assert (short.col_index, short.row_index, short.cell_ref) == (25, 20, 4)

    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=ctx,
        initializer=enable_crosshair_deal_table,
    ) as executor:
        col_index, row_index, cell_ref = executor.submit(worker_deal_maxima).result(timeout=30)

    assert (col_index, row_index, cell_ref) == (25, 20, 4)
    # Parent pytest process must keep the wide table.
    assert os.environ.get(CROSSHAIR_ENV) != "1"
    assert DEAL_MAX_COL_INDEX == 18277
    assert DEAL_MAX_ROW_INDEX == 1_048_575
    assert DEAL_MAX_CELL_REF == 32
