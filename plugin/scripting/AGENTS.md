# Scripting / LibrePy

Root **Do not redo** still applies. This file is the local map.

## Entry points

- Public script API, sandbox, venv worker (not for user imports): this package, `venv/`, `import_policy.py`, `sandbox.py`, `venv_worker.py`, `venv_diagnostics.py`
- LibrePy bootstrap: `plugin/main_core.py`, `plugin/librepy/`, `plugin/calc/python/addin_librepy.py`
- Bundle file list: `scripts/librepy_bundle_paths.py`

Topic docs: [docs/scripting/librepy-split.md](../../docs/scripting/librepy-split.md),
[docs/scripting/numpy-serialization.md](../../docs/scripting/numpy-serialization.md),
[docs/scripting/serialization-verification.md](../../docs/scripting/serialization-verification.md),
[docs/scripting/numpy-domains.md](../../docs/scripting/numpy-domains.md),
[docs/scripting/ms-py-compatibility.md](../../docs/scripting/ms-py-compatibility.md),
[docs/archive/scripting-domain-debt-dev-plan.md](../../docs/archive/scripting-domain-debt-dev-plan.md).

## Sharp edges

- Do **not** invent `python_config.py` or rename `writeragent.json` for LibrePy.
- Do **not** split `payload_codec.py` flatten/unpack without serialization A/B tests.
- Envelope-detector `@deal` + Hypothesis oracles on `payload_codec` (`is_split_grid`, `is_multi_data`, image / dataframe / calc_range) are **shipped**.
- Scripting domain registries (Phases 1–6) are shipped — do not add a fourth ad-hoc registry.
- `venv/calc_functions_*.py` alphabet splits are intentional; do not merge them.
- Do **not** slim `trusted_action_registry.py` / `venv_diagnostics.py` for LibrePy while those modules still work.
- Worker and editor pickle reads must pass `ipc.DEFAULT_MAX_PAYLOAD_BYTES`; do not call `read_frame_payload` unbounded. Same cap on the child harness, generated RPC (`writeragent_api` / `generate_tool_proxies`), ppt-master child IPC, and the compute-service stdio loop.
- Venv → LO tool RPC reuses the existing `tool_call` Pickle5 frame (`host_rpc.py`); do not add a second IPC protocol. `=PY()` recalc must pass `python_tool_domain=""` so formula evaluation cannot mutate the document. An empty allowlist also refuses `get_named_python_script` / `list_named_python_scripts` (those used to run before the gate and could read every stored script). Named libraries (`wa.scripts` / `wa.doc`) fetch stored script text on that pipe only when tool RPC is not disabled; do not call `run_venv_python_script` from a script.
- Shared kernel (`scripting.python_session_mode`) is the document-keyed cache for those libraries across runs; Isolated still caches for the duration of one execute.
- Do **not** drop `plugin/calc/analyzer.py` from the LibrePy bundle.
- Jupyter import (`plugin/notebook/`, vendored `plugin/contrib/nbformat/`) ships in LibrePy; do not exclude it from the allowlist.
- ``writeragent.X`` is allowed only when ``plugin.X`` is on ``VENV_AUTHORIZED_IMPORTS``, or the alias is an explicit entry (``writeragent.vision``, ``writeragent.scripting.duckdb_sql``). Do not put back a blanket ``writeragent.*``. Leave ``duckdb`` / ``duckdb.*`` on the list and ``plugin.scripting.duckdb_sql`` off it.
- Shipped LibrePy (`make deploy-core`) defaults to `log_level` WARN; a checkout that still has `plugin/tests/` defaults to DEBUG.
- Python sidebar header/hamburger (`plugin/librepy/sidebar_menus.py`) must not import `plugin.main`, `llm_client`, embeddings, or MCP.
- Python deck is Calc + Writer (not NotebookBar-only). Writer hides `=PY()` cell chrome; do not fall back to a Calc document from a Writer frame.
- `PythonPanelElement` is `XUIElement` only. Deck close runs `PythonSidebarController.disposing` from the root window listener's `on_dispose`, not from `PythonPanelElement.disposing`. `getRealInterface` must hop to the VCL thread (`_run_on_main_thread`); URP calls it off-main.
