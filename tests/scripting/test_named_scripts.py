from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.named_scripts import (
    GET_NAMED_PYTHON_SCRIPT,
    LIST_NAMED_PYTHON_SCRIPTS,
    ORIGIN_USER,
    extract_library_source,
    host_get_named_python_script,
    host_list_named_python_scripts,
    python_identifier_from_script_name,
    script_body_hash,
)


def test_host_list_omits_workbook_init_script():
    import pytest

    listed = host_list_named_python_scripts(
        user_scripts={"Mine": "a = 1"},
        document_scripts={"INIT": "b = 1", "Init": "c = 1", "Hello": "d = 1"},
    )
    assert listed["document"] == ["Hello"]
    with pytest.raises(RuntimeError, match="INIT"):
        host_get_named_python_script(
            name="INIT",
            origin="document",
            known_hash=None,
            user_scripts={},
            document_scripts={"INIT": "b = 1"},
        )


def test_python_identifier_from_script_name():
    assert python_identifier_from_script_name("Hello World") == "Hello_World"
    assert python_identifier_from_script_name("  spaced  ") == "spaced"
    assert python_identifier_from_script_name("123 go!") == "_123_go"
    assert python_identifier_from_script_name("class") == "class_"
    assert python_identifier_from_script_name("a---b") == "a_b"
    assert python_identifier_from_script_name("***") == "_script"
    assert python_identifier_from_script_name("") == "_script"


def test_extract_library_source_drops_toplevel_calls():
    src = extract_library_source(
        "def add(a, b):\n"
        "    return a + b\n"
        "K = 3\n"
        "print('nope')\n"
        "wa.writer.apply_document_content(content=['x'], target='end')\n"
    )
    assert "def add" in src
    assert "K = 3" in src
    assert "apply_document_content" not in src
    assert "print" not in src


def test_extract_library_source_keeps_computed_assigns_and_rejects_loops():
    src = extract_library_source("FACTOR = 2\nSCALE = FACTOR * 2\n")
    assert "SCALE = FACTOR * 2" in src
    with pytest.raises(ValueError, match="lines 2"):
        extract_library_source("FACTOR = 2\nfor i in range(3):\n    pass\n")


def test_host_get_named_python_script_hash_short_circuit():
    code = "def add(a, b):\n    return a + b\n"
    digest = script_body_hash(code)
    out = host_get_named_python_script(
        name="Helpers",
        origin=ORIGIN_USER,
        known_hash=digest,
        user_scripts={"Helpers": code},
        document_scripts={},
    )
    assert out["unchanged"] is True
    assert "code" not in out


def test_host_list_named_python_scripts_splits_origins():
    listing = host_list_named_python_scripts(
        user_scripts={"B": "1", "A": "2"},
        document_scripts={"Doc": "3"},
    )
    assert listing[ORIGIN_USER] == ["A", "B"]
    assert listing["document"] == ["Doc"]


def test_run_venv_python_script_still_blocked():
    from plugin.scripting.host_rpc import execute_tool

    try:
        execute_tool("run_venv_python_script", {"code": "1"})
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "re-enter" in str(exc)


def test_sandbox_library_second_attr_does_not_refetch():
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    code = (
        "def add(a, b):\n"
        "    return a + b\n"
        "def mul(a, b):\n"
        "    return a * b\n"
        "print('side effect')\n"
    )
    calls: list[tuple[str, dict]] = []

    def fake_rpc(tool: str, **kwargs):
        calls.append((tool, kwargs))
        if tool == LIST_NAMED_PYTHON_SCRIPTS:
            return {"user": ["Helpers"], "document": []}
        if tool == GET_NAMED_PYTHON_SCRIPT:
            return {
                "unchanged": False,
                "hash": script_body_hash(code),
                "name": "Helpers",
                "origin": "user",
                "code": code,
            }
        raise AssertionError(tool)

    with patch("plugin.scripting.named_scripts._rpc_named", side_effect=fake_rpc):
        res = run_sandboxed_code(
            "import writeragent as wa\n"
            "result = wa.scripts.Helpers.add(1, 2) + wa.scripts.Helpers.mul(3, 4)\n"
        )
    assert res.get("status") == "ok", res
    assert res.get("result") == 15
    gets = [c for c in calls if c[0] == GET_NAMED_PYTHON_SCRIPT]
    assert len(gets) == 1


def test_getitem_uses_stored_title():
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    code = "def ping():\n    return 'ok'\n"

    def fake_rpc(tool: str, **kwargs):
        if tool == LIST_NAMED_PYTHON_SCRIPTS:
            return {"user": ["odd name"], "document": []}
        if tool == GET_NAMED_PYTHON_SCRIPT:
            assert kwargs["name"] == "odd name"
            return {
                "unchanged": False,
                "hash": script_body_hash(code),
                "name": "odd name",
                "origin": "user",
                "code": code,
            }
        raise AssertionError(tool)

    with patch("plugin.scripting.named_scripts._rpc_named", side_effect=fake_rpc):
        res = run_sandboxed_code(
            "import writeragent as wa\n"
            "result = wa.scripts['odd name'].ping()\n"
        )
    assert res.get("status") == "ok", res
    assert res.get("result") == "ok"


def _helpers_rpc(bodies: dict[str, str], calls: list[tuple[str, dict]] | None = None):
    def fake_rpc(tool: str, **kwargs):
        if calls is not None:
            calls.append((tool, kwargs))
        if tool == LIST_NAMED_PYTHON_SCRIPTS:
            return {"user": list(bodies.keys()), "document": []}
        if tool == GET_NAMED_PYTHON_SCRIPT:
            name = str(kwargs.get("name") or "Helpers")
            code = bodies[name]
            digest = script_body_hash(code)
            if kwargs.get("known_hash") == digest:
                return {"unchanged": True, "hash": digest, "name": name, "origin": "user"}
            return {
                "unchanged": False,
                "hash": digest,
                "name": name,
                "origin": "user",
                "code": code,
            }
        raise AssertionError(tool)

    return fake_rpc


def test_shared_session_picks_up_script_edit():
    from plugin.scripting.venv.venv_sandbox import reset_sandbox_session, run_sandboxed_code

    bodies = {"Helpers": "def n():\n    return 1\n"}
    sid = "test-named-scripts-edit"
    try:
        with patch("plugin.scripting.named_scripts._rpc_named", side_effect=_helpers_rpc(bodies)):
            r1 = run_sandboxed_code(
                "import writeragent as wa\nresult = wa.scripts.Helpers.n()\n",
                session_id=sid,
            )
            assert r1.get("status") == "ok", r1
            assert r1.get("result") == 1
            bodies["Helpers"] = "def n():\n    return 2\n"
            r2 = run_sandboxed_code(
                "import writeragent as wa\nresult = wa.scripts.Helpers.n()\n",
                session_id=sid,
            )
        assert r2.get("status") == "ok", r2
        assert r2.get("result") == 2
    finally:
        reset_sandbox_session(sid)


def test_shared_session_unchanged_sends_hash_not_body():
    from plugin.scripting.venv.venv_sandbox import reset_sandbox_session, run_sandboxed_code

    bodies = {"Helpers": "def n():\n    return 1\n"}
    calls: list[tuple[str, dict]] = []
    sid = "test-named-scripts-hash"
    try:
        with patch("plugin.scripting.named_scripts._rpc_named", side_effect=_helpers_rpc(bodies, calls)):
            run_sandboxed_code(
                "import writeragent as wa\nresult = wa.scripts.Helpers.n()\n",
                session_id=sid,
            )
            calls.clear()
            res = run_sandboxed_code(
                "import writeragent as wa\nresult = wa.scripts.Helpers.n()\n",
                session_id=sid,
            )
        assert res.get("status") == "ok", res
        gets = [c for c in calls if c[0] == GET_NAMED_PYTHON_SCRIPT]
        assert len(gets) == 1
        assert gets[0][1].get("known_hash") == script_body_hash(bodies["Helpers"])
    finally:
        reset_sandbox_session(sid)


def test_doc_hello_function_call():
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    code = "def hello():\n    return 'Hello, Keith'\n"

    def fake_rpc(tool: str, **kwargs):
        if tool == LIST_NAMED_PYTHON_SCRIPTS:
            return {"user": [], "document": ["hello_writeragent"]}
        if tool == GET_NAMED_PYTHON_SCRIPT:
            return {
                "unchanged": False,
                "hash": script_body_hash(code),
                "name": "hello_writeragent",
                "origin": "document",
                "code": code,
            }
        raise AssertionError(tool)

    with patch("plugin.scripting.named_scripts._rpc_named", side_effect=fake_rpc):
        res = run_sandboxed_code(
            "import writeragent as wa\n"
            "result = wa.doc.hello_writeragent.hello()\n"
        )
    assert res.get("status") == "ok", res
    assert res.get("result") == "Hello, Keith"


def test_script_library_uses_bound_executor_not_contextvar():
    """Timeout fallback may eval off the bind thread; ContextVar would be empty."""
    from plugin.scripting.named_scripts import ORIGIN_DOCUMENT, ScriptLibrary, _current_executor
    from plugin.scripting.venv.venv_sandbox import _new_executor

    code = "def hello():\n    return 'Hello, Keith'\n"
    exe = _new_executor(10)
    lib = ScriptLibrary(ORIGIN_DOCUMENT)
    lib._executor = exe

    def fake_rpc(tool: str, **kwargs):
        if tool == LIST_NAMED_PYTHON_SCRIPTS:
            return {"user": [], "document": ["hello_writeragent"]}
        if tool == GET_NAMED_PYTHON_SCRIPT:
            return {
                "unchanged": False,
                "hash": script_body_hash(code),
                "name": "hello_writeragent",
                "origin": "document",
                "code": code,
            }
        raise AssertionError(tool)

    token = _current_executor.set(None)
    try:
        with patch("plugin.scripting.named_scripts._rpc_named", side_effect=fake_rpc):
            assert lib.hello_writeragent.hello() == "Hello, Keith"
    finally:
        _current_executor.reset(token)


def test_attach_binds_librepy_namespace_stub():
    from plugin.scripting import writeragent_namespace as ns
    from plugin.scripting.named_scripts import ScriptLibrary, attach_named_script_libraries
    from plugin.scripting.venv.venv_sandbox import _new_executor

    exe = _new_executor(10)
    attach_named_script_libraries(exe)
    assert isinstance(ns.scripts, ScriptLibrary)
    assert isinstance(ns.doc, ScriptLibrary)
    assert ns.doc._executor is exe


def test_sandbox_import_writeragent_without_api_has_doc():
    """LibrePy: import writeragent is the namespace stub; libraries must still bind."""
    import importlib.util
    import sys

    from plugin.framework.uno_bootstrap import _WRITERAGENT_API, register_alias_importer
    from plugin.scripting.venv.venv_sandbox import run_sandboxed_code

    orig_find = importlib.util.find_spec

    def find_spec_no_api(name: str, package=None):
        if name == _WRITERAGENT_API:
            return None
        return orig_find(name, package)

    for key in list(sys.modules):
        if key == "writeragent" or key.startswith("writeragent."):
            del sys.modules[key]
    try:
        register_alias_importer()
        with patch("importlib.util.find_spec", side_effect=find_spec_no_api):
            res = run_sandboxed_code(
                "import writeragent as wa\n"
                "result = bool(getattr(wa, 'doc', None) and getattr(wa, 'scripts', None))\n"
            )
        assert res.get("status") == "ok", res
        assert res.get("result") is True
    finally:
        for key in list(sys.modules):
            if key == "writeragent" or key.startswith("writeragent."):
                del sys.modules[key]


def test_rps_session_id_writer_is_document_keyed():
    from plugin.scripting import session_manager as sm

    ctx = MagicMock()
    doc_a = MagicMock()
    doc_a.getURL.return_value = "file:///a.odt"
    doc_b = MagicMock()
    doc_b.getURL.return_value = "file:///b.odt"
    with (
        patch.object(sm, "python_session_mode", return_value="shared"),
        patch.object(sm, "is_calc", return_value=False),
    ):
        sid_a = sm.rps_session_id(ctx, doc_a)
        sid_b = sm.rps_session_id(ctx, doc_b)
    assert sid_a == "rps:file:///a.odt"
    assert sid_b == "rps:file:///b.odt"
    assert sid_a != sid_b


def test_rps_session_id_isolated_is_none():
    from plugin.scripting import session_manager as sm

    ctx = MagicMock()
    doc = MagicMock()
    with patch.object(sm, "python_session_mode", return_value="isolated"):
        assert sm.rps_session_id(ctx, doc) is None


def test_host_rpc_named_script_allowed_when_tools_disabled():
    from plugin.scripting.host_rpc import TOOL_RPC_DISABLED, execute_tool, resolve_allowed_tools

    allowed = resolve_allowed_tools(TOOL_RPC_DISABLED)
    assert allowed == frozenset()
    code = "def add(a, b):\n    return a + b\n"
    with (
        patch("plugin.scripting.document_scripts.get_user_scripts", return_value={"Helpers": code}),
        patch("plugin.scripting.document_scripts.get_document_scripts", return_value={}),
        patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()),
        patch("plugin.framework.uno_context.get_active_document", return_value=MagicMock()),
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()),
    ):
        out = execute_tool(
            GET_NAMED_PYTHON_SCRIPT,
            {"name": "Helpers", "origin": "user"},
            allowed_tools=allowed,
        )
    assert out["code"] == code


def test_rpc_named_librepy_fallback_uses_exchange_tool_call():
    import builtins

    from plugin.scripting.named_scripts import _rpc_named

    real_import = builtins.__import__

    def _block_api(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "plugin.scripting.writeragent_api":
            raise ImportError("LibrePy omits writeragent_api")
        return real_import(name, globals, locals, fromlist, level)

    with (
        patch.dict("os.environ", {"WRITERAGENT_IS_WORKER": "1"}),
        patch("builtins.__import__", side_effect=_block_api),
        patch("plugin.scripting.ipc.exchange_tool_call", return_value={"body": "x"}) as mock_exchange,
    ):
        result = _rpc_named("get_named_python_script", name="Hello", missing=None)

    assert result == {"body": "x"}
    mock_exchange.assert_called_once_with("get_named_python_script", {"name": "Hello"})
