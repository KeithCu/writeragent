# WriterAgent tests for scripts/eval_2_flag_preview.py
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from eval_2_flag_preview import PREVIEW_PNG_NAME, export_preview_png, main  # noqa: E402
from eval_2_headed import PYTHON_SHAPES_STAMP_ODT  # noqa: E402


def test_missing_soffice_does_not_score(tmp_path: Path) -> None:
    source = tmp_path / PYTHON_SHAPES_STAMP_ODT
    source.write_bytes(b"odt")
    code, message = export_preview_png(source, tmp_path, soffice=None)
    assert code == 2
    assert "not change oracle pass/fail" in message
    assert not (tmp_path / PREVIEW_PNG_NAME).exists()


def test_export_writes_preview_png(tmp_path: Path) -> None:
    source = tmp_path / PYTHON_SHAPES_STAMP_ODT
    source.write_bytes(b"odt")

    def runner(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        (outdir / f"{source.stem}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    code, message = export_preview_png(
        source,
        tmp_path,
        soffice="/usr/bin/soffice",
        runner=runner,
    )
    assert code == 0
    assert (tmp_path / PREVIEW_PNG_NAME).is_file()
    assert "not scored" in message


def test_cli_missing_document(tmp_path: Path) -> None:
    assert main([str(tmp_path)]) == 2
