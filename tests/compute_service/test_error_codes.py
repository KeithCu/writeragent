from __future__ import annotations
"""The README HTTP status table lists every error code in the row of its status."""


import re
from pathlib import Path

from compute_service import error_codes

_README = Path(__file__).resolve().parents[2] / "compute_service" / "README.md"
_ROW = re.compile(r"^\| \*\*`(\d{3}) [^`]*`\*\*\s*\|(.*)$")
_CODE = re.compile(r"`([A-Z]+(?:_[A-Z]+)+)`")


def _readme_codes_by_status() -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for line in _README.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            rows[m.group(1)] = set(_CODE.findall(m.group(2)))
    return rows


def test_constants_name_their_own_code() -> None:
    for code in error_codes.HTTP_STATUS:
        assert getattr(error_codes, code) == code


def test_readme_status_table_matches_error_codes() -> None:
    rows = _readme_codes_by_status()
    expected: dict[str, set[str]] = {}
    for code, status in error_codes.HTTP_STATUS.items():
        expected.setdefault(status.split()[0], set()).add(code)
    for status, codes in expected.items():
        assert rows.get(status, set()) == codes, status
    for status, codes in rows.items():
        assert not codes or status in expected, (status, codes)
