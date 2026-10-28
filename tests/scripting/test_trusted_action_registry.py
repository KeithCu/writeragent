# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted_action_registry wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.trusted_action_registry import (
    TrustedActionWiring,
    _index_by_domain,
    _split_handler,
    get_trusted_action_wiring,
)


def test_get_trusted_action_wiring_known_domains() -> None:
    analysis = get_trusted_action_wiring("analysis")
    assert analysis is not None
    assert analysis.handler.endswith("trusted_dispatch:dispatch_spec_domain")

    embeddings = get_trusted_action_wiring("embeddings_index")
    assert embeddings is not None
    assert embeddings.supports_heartbeat is True

    languagetool = get_trusted_action_wiring("languagetool")
    assert languagetool is not None
    assert languagetool.handler.endswith("trusted_dispatch:dispatch_languagetool")


def test_all_trusted_action_wirings_resolve_and_match_heartbeat() -> None:
    import importlib
    import inspect
    from plugin.scripting.trusted_action_registry import _TRUSTED_ACTION_WIRING

    for wiring in _TRUSTED_ACTION_WIRING:
        mod_name, attr_name = _split_handler(wiring.handler)
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, attr_name)
        assert callable(fn), f"{wiring.handler} is not callable"
        sig = inspect.signature(fn)
        has_heartbeat_param = "heartbeat_fn" in sig.parameters
        assert has_heartbeat_param == wiring.supports_heartbeat, (
            f"{wiring.domain} ({wiring.handler}) supports_heartbeat={wiring.supports_heartbeat} "
            f"but heartbeat_fn in signature={has_heartbeat_param}"
        )


def test_get_trusted_action_wiring_unknown_domain() -> None:
    assert get_trusted_action_wiring("not_a_domain") is None


@pytest.mark.parametrize(
    "handler",
    [
        "plugin.scripting.venv.trusted_dispatch",
        "pkg:mod:attr",
        ":attr",
        "pkg:",
        ":",
        "",
    ],
)
def test_dispatch_rejects_handler_that_is_not_module_attr(handler: str) -> None:
    wiring = TrustedActionWiring("t", handler)
    with pytest.raises(ValueError, match="module:attr"):
        wiring.dispatch({})


def test_index_by_domain_rejects_duplicates() -> None:
    first = TrustedActionWiring("units", "plugin.scripting.venv.trusted_dispatch:dispatch_units")
    second = TrustedActionWiring("units", "plugin.scripting.venv.trusted_dispatch:dispatch_symbolic")
    with pytest.raises(ValueError, match="Duplicate trusted action domain"):
        _index_by_domain((first, second))


def test_dispatch_passes_heartbeat_only_when_supported() -> None:
    def no_heartbeat(data: dict[str, object]) -> str:
        return f"no:{data['k']}"

    def with_heartbeat(data: dict[str, object], *, heartbeat_fn: object = None) -> str:
        return f"yes:{heartbeat_fn is sentinel}"

    sentinel = object()
    mod = MagicMock()
    mod.no_heartbeat = no_heartbeat
    mod.with_heartbeat = with_heartbeat
    plain = TrustedActionWiring("t", "pkg:no_heartbeat", supports_heartbeat=False)
    beating = TrustedActionWiring("t", "pkg:with_heartbeat", supports_heartbeat=True)
    with patch("importlib.import_module", return_value=mod):
        assert plain.dispatch({"k": 1}, heartbeat_fn=sentinel) == "no:1"
        assert beating.dispatch({"k": 1}, heartbeat_fn=sentinel) == "yes:True"


def test_trusted_dispatch_import_does_not_load_host_worker() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo_root}:{existing_pp}" if existing_pp else repo_root

    child_code = (
        "import sys\n"
        "import plugin.scripting.venv.trusted_dispatch\n"
        "forbidden = [\n"
        "    'plugin.scripting.trusted_rpc',\n"
        "    'plugin.scripting.venv_worker',\n"
        "    'plugin.framework.config',\n"
        "    'plugin.framework.worker_pool',\n"
        "    'plugin.framework.thread_guard',\n"
        "    'plugin.scripting.sandbox',\n"
        "]\n"
        "loaded_forbidden = [m for m in forbidden if m in sys.modules]\n"
        "if 'plugin.scripting.sheet_layout' not in sys.modules:\n"
        "    sys.stderr.write('Missing plugin.scripting.sheet_layout in sys.modules\\n')\n"
        "    sys.exit(1)\n"
        "if loaded_forbidden:\n"
        "    sys.stderr.write(f'Forbidden modules loaded: {loaded_forbidden}\\n')\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Child failed (stdout={proc.stdout}, stderr={proc.stderr})"


def test_trusted_action_handlers_do_not_import_host_modules() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo_root}:{existing_pp}" if existing_pp else repo_root

    child_code = (
        "import ast\n"
        "import importlib\n"
        "import inspect\n"
        "import sys\n"
        "import textwrap\n"
        "from plugin.scripting.trusted_action_registry import _TRUSTED_ACTION_WIRING, _split_handler\n"
        "\n"
        "for wiring in _TRUSTED_ACTION_WIRING:\n"
        "    mod_name, attr_name = _split_handler(wiring.handler)\n"
        "    mod = importlib.import_module(mod_name)\n"
        "    fn = getattr(mod, attr_name)\n"
        "    source = textwrap.dedent(inspect.getsource(fn))\n"
        "    tree = ast.parse(source)\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, ast.Import):\n"
        "            for alias in node.names:\n"
        "                importlib.import_module(alias.name)\n"
        "        elif isinstance(node, ast.ImportFrom):\n"
        "            if node.level != 0:\n"
        "                sys.stderr.write(f'Relative import in {wiring.handler}: level={node.level}\\n')\n"
        "                sys.exit(1)\n"
        "            if node.module:\n"
        "                importlib.import_module(node.module)\n"
        "\n"
        "forbidden = {\n"
        "    'plugin.scripting.trusted_rpc',\n"
        "    'plugin.scripting.venv_worker',\n"
        "    'plugin.framework.config',\n"
        "    'plugin.framework.config_schema',\n"
        "    'plugin.framework.worker_pool',\n"
        "    'plugin.framework.thread_guard',\n"
        "    'plugin.scripting.sandbox',\n"
        "    'uno',\n"
        "    'unohelper',\n"
        "}\n"
        "loaded_forbidden = [\n"
        "    m for m in sys.modules\n"
        "    if m in forbidden or m == 'plugin.writer' or m.startswith('plugin.writer.')\n"
        "]\n"
        "if loaded_forbidden:\n"
        "    sys.stderr.write(f'Forbidden modules loaded: {sorted(loaded_forbidden)}\\n')\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"Child failed (stdout={proc.stdout}, stderr={proc.stderr})"

