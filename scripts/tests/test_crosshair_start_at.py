# WriterAgent tests — CrossHair --start-at resume index
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.crosshair_check_all import apply_start_at, start_at_status_line
from scripts.crosshair_stream import StreamStats

_DEAL_SRC = "import deal\n@deal.post(lambda result, *_, **__: True)\ndef f():\n    return 1\n"


def _write_deal_modules(plugin: Path, names: tuple[str, ...]) -> list[Path]:
    plugin.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name in names:
        path = plugin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEAL_SRC, encoding="utf-8")
        paths.append(path)
    return paths


def test_apply_start_at_identity() -> None:
    files = [Path(f"plugin/m{i}.py") for i in range(1, 6)]
    assert apply_start_at(files, 1) is files
    assert apply_start_at([], 1) == []


def test_apply_start_at_42_drops_first_41() -> None:
    files = [Path(f"plugin/m{i:02d}.py") for i in range(1, 57)]
    sliced = apply_start_at(files, 42)
    assert len(sliced) == 15
    assert sliced[0] == files[41]
    assert sliced[-1] == files[-1]
    assert start_at_status_line(42, 56) == "starting at module 42/56 (skipped 41)"


def test_apply_start_at_rejects_below_one() -> None:
    files = [Path("plugin/a.py")]
    with pytest.raises(ValueError, match=r"--start-at must be >= 1 \(got 0\)"):
        apply_start_at(files, 0)
    with pytest.raises(ValueError, match=r"--start-at must be >= 1"):
        apply_start_at(files, -3)


def test_apply_start_at_rejects_past_last() -> None:
    files = [Path(f"plugin/m{i}.py") for i in range(3)]
    with pytest.raises(ValueError, match=r"--start-at 4 is past the last module \(3\)"):
        apply_start_at(files, 4)


def test_apply_start_at_last_index_keeps_one() -> None:
    files = [Path("plugin/a.py"), Path("plugin/b.py"), Path("plugin/c.py")]
    assert apply_start_at(files, 3) == [files[2]]


def test_cover_start_at_slices_submit_order_not_discovery() -> None:
    """Cover --start-at N is the Nth module in order_cover_targets, not check-all sort."""
    from scripts.crosshair_cover_all import order_cover_targets

    short = Path("plugin/chatbot/research_cache_fluff.py")
    mid = Path("plugin/scripting/payload_codec.py")
    long = Path("plugin/mcp/mcp_state.py")
    unknown = Path("plugin/zzz/new_deal_module.py")
    # Discovery-style alpha: fluff, mcp, payload, zzz. Cover submit: zzz, mcp, payload, fluff.
    ordered = order_cover_targets([short, unknown, mid, long])
    sliced = apply_start_at(ordered, 2)
    assert [p.as_posix() for p in sliced] == [
        "plugin/mcp/mcp_state.py",
        "plugin/scripting/payload_codec.py",
        "plugin/chatbot/research_cache_fluff.py",
    ]


def test_check_all_list_numbers_rows_and_ignores_start_at(tmp_path, capsys, monkeypatch) -> None:
    from plugin.framework.deal_shim import CROSSHAIR_ENV
    from scripts.crosshair_check_all import main as check_all_main

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)
    plugin = tmp_path / "plugin"
    _write_deal_modules(plugin, ("a_mod.py", "b_mod.py", "c_mod.py"))

    code = check_all_main(["--list", "--start-at", "99", "--plugin-root", str(plugin)])
    assert code == 0
    out = capsys.readouterr().out
    assert "  1  " in out and "a_mod.py" in out
    assert "  2  " in out and "b_mod.py" in out
    assert "  3  " in out and "c_mod.py" in out
    assert "starting at module" not in out


def test_cover_all_list_numbers_rows_and_ignores_start_at(tmp_path, capsys, monkeypatch) -> None:
    from plugin.framework.deal_shim import CROSSHAIR_ENV
    from scripts.crosshair_cover_all import main as cover_all_main

    monkeypatch.delenv(CROSSHAIR_ENV, raising=False)
    plugin = tmp_path / "plugin"
    _write_deal_modules(
        plugin,
        (
            "chatbot/research_cache_fluff.py",
            "mcp/mcp_state.py",
            "scripting/payload_codec.py",
        ),
    )

    code = cover_all_main(["--list", "--start-at", "2", "--plugin-root", str(plugin)])
    assert code == 0
    out = capsys.readouterr().out
    # Submit order: mcp_state (longest known), payload_codec, fluff — all three listed.
    assert out.index("  1  ") < out.index("mcp_state.py")
    assert "  2  " in out and "payload_codec.py" in out
    assert "  3  " in out and "research_cache_fluff.py" in out
    assert "starting at module" not in out


def test_check_all_start_at_zero_exits_2(tmp_path, capsys) -> None:
    from scripts.crosshair_check_all import main as check_all_main

    plugin = tmp_path / "plugin"
    _write_deal_modules(plugin, ("a_mod.py",))
    code = check_all_main(["--start-at", "0", "--plugin-root", str(plugin)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--start-at must be >= 1" in err


def test_cover_all_start_at_zero_exits_2(tmp_path, capsys) -> None:
    from scripts.crosshair_cover_all import main as cover_all_main

    plugin = tmp_path / "plugin"
    _write_deal_modules(plugin, ("a_mod.py",))
    code = cover_all_main(["--start-at", "0", "--plugin-root", str(plugin)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--start-at must be >= 1" in err


def test_check_all_start_at_past_last_names_max(tmp_path, capsys) -> None:
    from scripts.crosshair_check_all import main as check_all_main

    plugin = tmp_path / "plugin"
    _write_deal_modules(plugin, ("a_mod.py", "b_mod.py"))
    code = check_all_main(["--start-at", "3", "--plugin-root", str(plugin)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--start-at 3 is past the last module (2)" in err


def test_cover_all_start_at_past_last_names_max(tmp_path, capsys) -> None:
    from scripts.crosshair_cover_all import main as cover_all_main

    plugin = tmp_path / "plugin"
    _write_deal_modules(plugin, ("a_mod.py", "b_mod.py"))
    code = cover_all_main(["--start-at", "9", "--plugin-root", str(plugin)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--start-at 9 is past the last module (2)" in err


def test_check_all_start_at_keeps_original_index(tmp_path, capsys, monkeypatch) -> None:
    """Restart at 2 of 3 still prints [2/3], not [1/2]. No live CrossHair."""
    from scripts.crosshair_check_all import main as check_all_main

    monkeypatch.setattr(
        "scripts.crosshair_check_all.run_crosshair",
        lambda *args, **kwargs: (0, StreamStats()),
    )
    plugin = tmp_path / "plugin"
    _write_deal_modules(plugin, ("a_mod.py", "b_mod.py", "c_mod.py"))
    log = tmp_path / "check.log"
    code = check_all_main(
        ["--plugin-root", str(plugin), "--start-at", "2", "--log", str(log)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "starting at module 2/3 (skipped 1)" in out
    assert "[2/3]" in out
    assert "[3/3]" in out
    assert "[1/3]" not in out
    assert "[1/2]" not in out
    text = log.read_text(encoding="utf-8")
    assert "[2/3]" in text and "[3/3]" in text
    assert "a_mod.py" not in text


def test_check_all_explicit_paths_honor_start_at(tmp_path, capsys, monkeypatch) -> None:
    from scripts.crosshair_check_all import main as check_all_main

    monkeypatch.setattr(
        "scripts.crosshair_check_all.run_crosshair",
        lambda *args, **kwargs: (0, StreamStats()),
    )
    plugin = tmp_path / "plugin"
    paths = _write_deal_modules(plugin, ("a_mod.py", "b_mod.py", "c_mod.py"))
    log = tmp_path / "check.log"
    # Explicit order is c, a, b; --start-at 2 skips c and keeps original [2/3].
    code = check_all_main(
        [str(paths[2]), str(paths[0]), str(paths[1]), "--start-at", "2", "--log", str(log)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "starting at module 2/3 (skipped 1)" in out
    assert "[2/3]" in out
    assert "[3/3]" in out
    assert "[1/3]" not in out
    text = log.read_text(encoding="utf-8")
    assert "c_mod.py" not in text
    assert "a_mod.py" in text
    assert "b_mod.py" in text


def test_cover_all_start_at_keeps_submit_index_finish_order_banner(
    tmp_path, capsys, monkeypatch
) -> None:
    """Skip first submit slot; CoverModuleResult.index stays 2/3; banners stay finish-order."""
    from concurrent.futures import Future

    from scripts.crosshair_cover_all import (
        PROGRESS_SENTINEL,
        CoverModuleResult,
        main as cover_all_main,
    )

    captured: list[tuple[str, int, int]] = []

    class RecordingPool:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> RecordingPool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(
            self, fn: object, rel: str, index: int, total: int, **kwargs: object
        ) -> Future[CoverModuleResult]:
            captured.append((rel, index, total))
            result = CoverModuleResult(
                rel=rel,
                index=index,
                total=total,
                exit_code=0,
                examples=0,
                explore=0,
                error_details=(),
                formatted=f"######## {PROGRESS_SENTINEL} {rel} ########\n",
                duration_sec=0.1,
            )
            fut: Future[CoverModuleResult] = Future()
            fut.set_result(result)
            return fut

    monkeypatch.setattr("scripts.crosshair_cover_all.ProcessPoolExecutor", RecordingPool)
    plugin = tmp_path / "plugin"
    _write_deal_modules(plugin, ("a_mod.py", "b_mod.py", "c_mod.py"))
    log = tmp_path / "cover.log"
    timings = tmp_path / "timings.json"
    code = cover_all_main(
        [
            "--plugin-root",
            str(plugin),
            "--start-at",
            "2",
            "--log",
            str(log),
            "--timings-json",
            str(timings),
            "--jobs",
            "1",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "starting at module 2/3 (skipped 1)" in out
    # Submit indices stay original 2, 3; remaining count is the banner denominator.
    assert [idx for _rel, idx, _total in captured] == [2, 3]
    assert all(total == 2 for _rel, _idx, total in captured)
    assert "a_mod.py" not in "".join(rel for rel, _idx, _total in captured)
    # Live banners are finish-order of the remaining set, not reindexed submit [1/2] vs [2/3].
    assert "[1/2]" in out
    assert "[2/2]" in out
    assert "[2/3]" not in out

