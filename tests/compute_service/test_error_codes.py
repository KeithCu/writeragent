"""The README HTTP status table lists every error code in the row of its status."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from compute_service import error_codes

_README = Path(__file__).resolve().parents[2] / "compute_service" / "README.md"
_ROW = re.compile(r"^\| \*\*`(\d{3}) [^`]*`\*\*\s*\|([^|]*)\|")
_CODE = re.compile(r"`([A-Z]+(?:_[A-Z]+)*)`")


def _readme_codes_by_status() -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for line in _README.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            rows[m.group(1)] = set(_CODE.findall(m.group(2)))
    return rows


def test_config_import_does_not_load_warm_child() -> None:
    script = (
        "import compute_service.config\n"
        "import sys\n"
        "assert 'plugin.framework.warm_child' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
    assert proc.returncode == 0


def test_readme_status_table_matches_error_codes() -> None:
    rows = _readme_codes_by_status()
    expected: dict[str, set[str]] = {}
    for code, status in error_codes.HTTP_STATUS.items():
        expected.setdefault(status.split()[0], set()).add(code)
    for status, codes in expected.items():
        assert rows.get(status, set()) == codes, status
    for status, codes in rows.items():
        assert not codes or status in expected, (status, codes)
