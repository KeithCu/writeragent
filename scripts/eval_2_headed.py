#!/usr/bin/env python3
# WriterAgent - headed eval-2 / AFC tool-round budget
"""Temporarily set chatbot.max_tool_rounds to 50 for headed eval-2 trials.

No new config knobs. Everyday chat stays at the schema default (15). This
script writes 50 into the existing writeragent.json, then restores the
previous file bytes when the run finishes (Enter or Ctrl-C).

Usage:
  .venv/bin/python scripts/eval_2_headed.py
  .venv/bin/python scripts/eval_2_headed.py --no-launch
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_TOOL_ROUNDS_KEY = "chatbot.max_tool_rounds"
EVAL_2_MAX_TOOL_ROUNDS = 50
_AFC_DIR = REPO_ROOT / "docs" / "eval" / "eval-2" / "afc-sample-83d10b06"
_FIXTURE_CANDIDATES = (
    _AFC_DIR / "fixtures" / "Population v2.ods",
    _AFC_DIR / "fixtures" / "Population v2.xlsx",
)


def writeragent_json_candidates() -> list[Path]:
    """Usual LibreOffice profile locations for writeragent.json."""
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", ""))
        return [appdata / "LibreOffice" / "4" / "user" / "writeragent.json"]
    if sys.platform == "darwin":
        return [
            Path("~/Library/Application Support/LibreOffice/4/user/writeragent.json").expanduser(),
        ]
    return [
        Path("~/.config/libreoffice/4/user/writeragent.json").expanduser(),
        Path("~/.config/libreoffice/4/user/config/writeragent.json").expanduser(),
        Path("~/.config/libreoffice/24/user/writeragent.json").expanduser(),
        Path("~/.config/libreoffice/24/user/config/writeragent.json").expanduser(),
    ]


def find_writeragent_json(
    explicit: Path | None = None,
    candidates: list[Path] | None = None,
) -> Path:
    if explicit is not None:
        return explicit
    search = writeragent_json_candidates() if candidates is None else candidates
    for path in search:
        if path.is_file():
            return path
    raise FileNotFoundError(
        "Could not find writeragent.json. Pass --config PATH "
        "(LibreOffice user profile)."
    )


def _parse_config_object(text: str) -> dict[str, object]:
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].lstrip(" \t")
        if stripped == "" or stripped.startswith("//"):
            idx += 1
            continue
        break
    data = json.loads("".join(lines[idx:]))
    if not isinstance(data, dict):
        raise ValueError("writeragent.json must be a JSON object")
    return data


@contextmanager
def temporary_max_tool_rounds(
    config_path: Path,
    rounds: int = EVAL_2_MAX_TOOL_ROUNDS,
) -> Iterator[Path]:
    """Set chatbot.max_tool_rounds for the block; restore prior file bytes after."""
    existed = config_path.is_file()
    original = config_path.read_text(encoding="utf-8") if existed else None
    try:
        data = _parse_config_object(original) if original else {}
        data[MAX_TOOL_ROUNDS_KEY] = rounds
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
        yield config_path
    finally:
        if original is None:
            if config_path.is_file() and not existed:
                config_path.unlink()
        else:
            config_path.write_text(original, encoding="utf-8")


def find_afc_fixture() -> Path | None:
    for path in _FIXTURE_CANDIDATES:
        if path.is_file():
            return path
    return None


def launch_calc(fixture: Path | None) -> None:
    soffice = shutil.which("soffice")
    if soffice is None:
        print("soffice not on PATH; open Calc yourself.", file=sys.stderr)
        return
    cmd = [soffice, "--calc"]
    if fixture is not None:
        cmd.append(str(fixture))
    subprocess.Popen(cmd)


def _wait_for_finish() -> None:
    prompt = (
        f"eval-2 headed: {MAX_TOOL_ROUNDS_KEY}={EVAL_2_MAX_TOOL_ROUNDS}. "
        "Press Enter when the run is finished to restore the previous value.\n"
    )
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="writeragent.json path")
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Only bump+restore config; do not start soffice",
    )
    args = parser.parse_args(argv)

    config_path = find_writeragent_json(args.config)
    fixture = find_afc_fixture()
    with temporary_max_tool_rounds(config_path):
        print(f"Using {config_path}: {MAX_TOOL_ROUNDS_KEY}={EVAL_2_MAX_TOOL_ROUNDS}")
        if not args.no_launch:
            launch_calc(fixture)
        _wait_for_finish()
    print(f"Restored previous {MAX_TOOL_ROUNDS_KEY} in {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
