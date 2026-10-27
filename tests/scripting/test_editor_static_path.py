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
    assert monaco_static_path("/vs/", str(assets), str(root)) is None
    assert monaco_static_path("/", str(assets), str(root)) == os.path.join(str(assets), "index.html")
    assert monaco_static_path("/nope", str(assets), str(root)) is None


def test_every_script_in_index_html_is_served(tmp_path):
    # index.html script tags must map to files the wsgi app serves, or the
    # editor loads without its shell/backend.
    import re
    from pathlib import Path

    assets = Path(__file__).resolve().parents[2] / "plugin/contrib/scripting/assets/editor"
    html = (assets / "index.html").read_text(encoding="utf-8")
    srcs = re.findall(r'<script src="([^"]+)"', html)
    assert "backend_monaco.js" in srcs and "editor_shell.js" in srcs
    assert srcs.index("backend_monaco.js") < srcs.index("editor_shell.js")
    for src in srcs:
        if src.startswith("vs/"):
            continue
        served = monaco_static_path("/" + src, str(assets), str(tmp_path))
        assert served is not None, src
        assert Path(served).is_file(), src
