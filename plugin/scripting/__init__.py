"""Host-side Python scripting: Run Python Script, ``=PY()`` venv, trusted helpers.

Public modules (import these; do not re-export from this package):

- ``python_runner`` — Tools → Run Python Script (dialog, execute, insert)
- ``client`` — trusted helper RPC into the warm venv (``run_analysis``, ``run_viz``, …)
- ``venv_worker`` — warm subprocess IPC; ``run_code_in_user_venv``
- ``sandbox`` / ``import_policy`` — spawn env, interpreter resolve, AST blocklist
- ``domain_registry`` — RPS post-venv routing and script-picker templates
- ``document_scripts`` — named scripts and Calc init on UserDefinedProperties

Compute for helpers lives in ``plugin.scripting.venv`` (worker process). Host
modules stay import-light for LibreOffice and LibrePy. Settings keys are in
``module.yaml``.
"""
