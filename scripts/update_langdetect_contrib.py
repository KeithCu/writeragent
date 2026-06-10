#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch langdetect, strip profiles to grammar-registry languages, install under plugin/contrib/langdetect."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEST_DIR = REPO_ROOT / "plugin" / "contrib" / "langdetect"
LANGDETECT_VERSION = "1.0.9"

# Subpackages to copy from upstream langdetect/ (exclude tests).
_COPY_DIRS = ("utils", "profiles")
_COPY_FILES = (
    "__init__.py",
    "detector.py",
    "detector_factory.py",
    "lang_detect_exception.py",
    "language.py",
)

def _patch_detector_py(content: str) -> str:
    content = re.sub(
        r"import random\nimport re\n\nimport six\nfrom six\.moves import zip, xrange\n",
        "import random\nimport re\n",
        content,
        count=1,
    )
    content = content.replace("xrange(", "range(")
    content = content.replace("elif ch >= six.u('\\u0300')", "elif ord(ch) >= 0x0300")
    content = content.replace("if ch >= six.u('\\u0080'):", "if ord(ch) >= 0x0080:")
    content = re.sub(r"^(\s+)six\.print_.*$", r"\1pass", content, flags=re.MULTILINE)
    if "import six" in content or "six." in content:
        raise SystemExit("detector.py still references six after patch")
    return content


def _ensure_repo_on_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _fetch_wheel(temp_dir: Path) -> Path:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        f"langdetect=={LANGDETECT_VERSION}",
        "--no-deps",
        "-d",
        str(temp_dir),
    ]
    print("Downloading", f"langdetect=={LANGDETECT_VERSION}", "...")
    try:
        subprocess.run(cmd, check=True, cwd=REPO_ROOT)
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback when system/venv python has no pip (dev machines use curl + PyPI JSON).
        import json
        import urllib.request

        meta_url = f"https://pypi.org/pypi/langdetect/{LANGDETECT_VERSION}/json"
        with urllib.request.urlopen(meta_url, timeout=60) as resp:
            meta = json.load(resp)
        wheels = [u for u in meta.get("urls", []) if u.get("packagetype") == "bdist_wheel"]
        if not wheels:
            raise SystemExit("No wheel URL in PyPI metadata for langdetect")
        wheel_url = wheels[0]["url"]
        dest = temp_dir / Path(wheel_url).name
        print("Downloading via urllib:", wheel_url)
        urllib.request.urlretrieve(wheel_url, dest)
    wheels = sorted(temp_dir.glob("langdetect-*.whl"))
    if not wheels:
        raise SystemExit("No langdetect wheel found after pip download")
    return wheels[-1]


def _write_readme(allowed: frozenset[str]) -> None:
    profiles = ", ".join(sorted(allowed))
    (DEST_DIR / "README.md").write_text(
        f"""# Vendored langdetect

Subset of [Mimino666/langdetect](https://github.com/Mimino666/langdetect) v{LANGDETECT_VERSION} (MIT).

**Profile allowlist:** grammar proofreader BCP-47 registry only ({len(allowed)} profiles):

`{profiles}`

**Re-sync from PyPI:**

```bash
make langdetect-contrib
```

**Merge policy:** Refresh via `scripts/update_langdetect_contrib.py` only; do not hand-edit upstream modules except the documented Py3-only `detector.py` patch (drops `six`).
""",
        encoding="utf-8",
    )


def update_langdetect_contrib(*, wheel_path: Path | None = None) -> None:
    _ensure_repo_on_path()
    from plugin.writer.locale.grammar_proofread_locale import langdetect_profiles_for_grammar_registry

    allowed = langdetect_profiles_for_grammar_registry()
    if len(allowed) < 2:
        raise SystemExit("Need at least 2 langdetect profiles for the detector factory")

    if DEST_DIR.exists():
        shutil.rmtree(DEST_DIR)
    DEST_DIR.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="langdetect-fetch-") as tmp:
        temp_dir = Path(tmp)
        wheel = wheel_path or _fetch_wheel(temp_dir)
        print("Extracting", wheel.name, "...")
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(temp_dir / "extract")
        src_root = temp_dir / "extract" / "langdetect"
        if not src_root.is_dir():
            raise SystemExit(f"Unexpected wheel layout: {src_root} missing")

        profiles_src = src_root / "profiles"
        if not profiles_src.is_dir():
            raise SystemExit("profiles/ missing in upstream wheel")

        upstream_profiles = {p.name for p in profiles_src.iterdir() if p.is_file() and not p.name.startswith(".")}
        missing = allowed - upstream_profiles
        if missing:
            raise SystemExit(f"Upstream wheel missing required profiles: {sorted(missing)}")

        for name in _COPY_FILES:
            src = src_root / name
            if not src.is_file():
                raise SystemExit(f"Missing upstream file: {name}")
            text = src.read_text(encoding="utf-8")
            if name == "detector.py":
                text = _patch_detector_py(text)
            (DEST_DIR / name).write_text(text, encoding="utf-8")

        for dirname in _COPY_DIRS:
            src = src_root / dirname
            if not src.is_dir():
                raise SystemExit(f"Missing upstream directory: {dirname}")
            dest = DEST_DIR / dirname
            dest.mkdir(parents=True)
            if dirname == "profiles":
                kept = 0
                for prof in sorted(allowed):
                    shutil.copy2(profiles_src / prof, dest / prof)
                    kept += 1
                print(f"Kept {kept} profiles (dropped {len(upstream_profiles) - kept} unused)")
            else:
                shutil.copytree(src, dest, dirs_exist_ok=True)

    _write_readme(allowed)
    print("Installed stripped langdetect to", DEST_DIR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh plugin/contrib/langdetect from PyPI")
    parser.add_argument(
        "wheel",
        nargs="?",
        type=Path,
        help="Optional path to a langdetect wheel (skip pip download)",
    )
    args = parser.parse_args()
    update_langdetect_contrib(wheel_path=args.wheel)


if __name__ == "__main__":
    main()
