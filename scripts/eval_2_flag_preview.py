#!/usr/bin/env python3
# WriterAgent - eval-2 / post-process preview PNG for the shapes flag
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export one preview PNG per python-shapes flag stamp. Not a scorer.

Eval-2 has no PNG export helper. Headed screenshots in the failure
autopsy were taken by hand. Product page render
(``plugin/writer/get_image.py`` ``_render_draw_page_png``) needs a live
UNO document and is not used here.

The stamp's saved Writer document (``final_flag.odt``, or
``American Flag.odt``) is the render source. This script asks
``soffice --headless --convert-to png`` for one ``preview.png`` beside
that file. A board gallery is ``runs/*/preview.png`` (one image per
model×run). A missing PNG does not change oracle pass/fail.

Usage:
  .venv/bin/python scripts/eval_2_flag_preview.py docs/eval/eval-2/python-shapes-flag/runs/<stamp>/
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from eval_2_python_shapes_flag_oracle import resolve_flag_artifact

PREVIEW_PNG_NAME = "preview.png"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def export_preview_png(
    source: Path,
    dest_dir: Path,
    *,
    soffice: str | None,
    runner: Runner = subprocess.run,
) -> tuple[int, str]:
    """Write ``preview.png`` from a saved ``.odt``. Return (code, message).

    Exit 0 when the PNG is written. Exit 2 when soffice is missing or the
    convert fails. Never consults the oracle.
    """
    convert = (
        f"soffice --headless --convert-to png --outdir {dest_dir} {source}"
    )
    if not soffice:
        return 2, (
            "No eval-2 PNG export helper. Saved document is the render source: "
            f"{source}. Re-run when soffice is on PATH ({convert}). "
            "A missing preview does not change oracle pass/fail."
        )
    dest_dir = dest_dir.expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / PREVIEW_PNG_NAME
    with tempfile.TemporaryDirectory(prefix="writeragent-flag-preview-") as tmp:
        cmd = [
            soffice,
            "--headless",
            "--convert-to",
            "png",
            "--outdir",
            tmp,
            str(source),
        ]
        proc = runner(cmd, capture_output=True, text=True, timeout=180, check=False)
        produced = Path(tmp) / f"{source.stem}.png"
        if proc.returncode != 0 or not produced.is_file():
            detail = (proc.stderr or proc.stdout or "").strip()
            return 2, (
                f"soffice did not write a PNG for {source} (exit {proc.returncode}). "
                f"{detail}".rstrip()
            )
        shutil.copy2(produced, dest)
    return 0, f"Wrote {dest} from {source.name} (not scored)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact",
        type=Path,
        help="Run directory or saved Writer .odt",
    )
    args = parser.parse_args(argv)
    source, _log = resolve_flag_artifact(args.artifact)
    if source is None:
        print(f"need a Writer .odt (artifact={args.artifact})")
        return 2
    code, message = export_preview_png(
        source,
        source.parent,
        soffice=shutil.which("soffice"),
    )
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
