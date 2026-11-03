"""LibrePy manifest generation filters WriterAgent-only config keys."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from generate_manifest import _filter_librepy_config  # noqa: E402


def test_filter_librepy_config_drops_librepy_exclude_fields():
    config = {
        "python_venv_path": {"type": "string", "widget": "text"},
        "ppt_master_data_path": {"type": "string", "widget": "folder", "librepy_exclude": True},
        "test_ppt_master_data": {"type": "string", "widget": "button", "librepy_exclude": True},
    }
    filtered = _filter_librepy_config(config)
    assert set(filtered) == {"python_venv_path"}
