"""run_timed must decode child UTF-8 even when the host locale is cp1252."""

from __future__ import annotations

import sys

from scripts.run_timed import main

_LIGHTBULB = "\U0001f4a1"


def test_run_timed_captures_utf8_child_output(capsys) -> None:
    code = "import sys; sys.stdout.buffer.write('\\U0001f4a1'.encode('utf-8'))"
    rc = main(["pyspector", sys.executable, "-c", code])
    assert rc == 0
    assert _LIGHTBULB in capsys.readouterr().out
