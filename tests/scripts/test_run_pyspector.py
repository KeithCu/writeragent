"""Windows typecheck: PySpector banner must not crash on cp1252 stdio."""

from __future__ import annotations

import io
import sys

from scripts.run_pyspector import _configure_utf8_stdio

_LIGHTBULB = "\U0001f4a1"


def test_configure_utf8_stdio_allows_pyspector_banner_emoji(monkeypatch) -> None:
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    stream.write("ok\n")
    stream.flush()
    try:
        stream.write(_LIGHTBULB)
        stream.flush()
        raised = False
    except UnicodeEncodeError:
        raised = True
    assert raised is True

    _configure_utf8_stdio()
    stream.write(_LIGHTBULB)
    stream.flush()
    assert _LIGHTBULB.encode("utf-8") in buf.getvalue()
