# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco static paths stay under the editor asset roots."""

from __future__ import annotations

import os

from plugin.scripting.venv.editor_main import monaco_static_path


def test_vs_path_cannot_escape_rocher_root(tmp_path):
    root = tmp_path / "vs"
    root.mkdir()
    (root / "loader.js").write_text("ok", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("x", encoding="utf-8")
    got = monaco_static_path("/vs/loader.js", str(assets), str(root))
    assert got == os.path.realpath(root / "loader.js")
    assert monaco_static_path("/vs/../../secret.txt", str(assets), str(root)) is None
    assert monaco_static_path("/vs//etc/passwd", str(assets), str(root)) is None
    assert monaco_static_path("/", str(assets), str(root)) == os.path.join(str(assets), "index.html")
    assert monaco_static_path("/nope", str(assets), str(root)) is None
