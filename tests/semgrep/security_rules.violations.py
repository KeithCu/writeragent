# Opengrep fixture: vendored security rules should flag these patterns.
from __future__ import annotations

import subprocess
import sys
import tempfile


def _bad_subprocess_shell():
    subprocess.call("echo {}".format(sys.argv[0]), shell=True)


def _bad_mktemp():
    return tempfile.mktemp()
