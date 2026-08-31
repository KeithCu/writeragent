#!/usr/bin/env python3
"""Run PySpector on plugin/ with project FP rules disabled.

Used by ``make pyspector`` / ``make pyspector-report`` (``make pyspector`` is part of make typecheck / make test).
Disables rules reviewed as false positives or accepted known risks for WriterAgent.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Optional

# Keep AST disk cache out of plugin/; PySpector hardcodes <scan>/.pyspector_cache.
# tempfile.gettempdir() is /tmp on Unix and %TEMP% on Windows.
_AST_CACHE_DIR = Path(tempfile.gettempdir()) / "writeragent-pyspector-cache" / "ast"

# Rule IDs reviewed as false positives or accepted known risks for WriterAgent.
_DISABLED_RULE_IDS = (
    "PY101",  # Tool.execute / chart helpers mistaken for SQL cursor.execute
    "PATH813",  # controlled cache/gallery mkdir/unlink/read
    "IMPORT825",  # fixed UNO / sys __import__ strings
    "GETATTR828",  # log_level allowlisted in resolve_log_level
    "REGEX870",  # short Calc sheet-ref regex; not attacker-controlled megabytes
    "PY002",  # pickle on trusted host↔venv IPC pipe (# nosec)
    "TLS001",  # optional verify-off for local/self-signed LLM endpoints
    "PY102",  # pdfinfo argv-list; local fidelity helper
    "ZIPSLIP001",  # audio_source.zip from fixed GitHub contrib URL only
)


def _configure_utf8_stdio() -> None:
    """Pin stdout/stderr to UTF-8 so PySpector's emoji banner can print on Windows.

    WHAT WAS WRONG: ``make typecheck`` on Windows CI crashed in pyspector
    ``_print_banner`` with ``UnicodeEncodeError: 'charmap' codec can't encode
    character '\\U0001f4a1'`` (cp1252).

    HOW IT HAPPENED: GitHub's Windows runner leaves Python stdio on the ANSI
    code page. The banner is cosmetic; the encode happens before any scan.

    WHY THIS FIXES IT: ``TextIOWrapper.reconfigure`` switches the existing
    handles to UTF-8. Tests (and any StringIO stub) have no ``reconfigure``.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _inject_disabled_rules(rules_toml: str) -> str:
    """Extend the built-in [defaults].disabled_rule_ids list."""
    marker = "disabled_rule_ids = ["
    idx = rules_toml.find(marker)
    if idx < 0:
        extra = ",\n".join(f'  "{rid}",' for rid in _DISABLED_RULE_IDS)
        return rules_toml + f"\n[defaults]\ndisabled_rule_ids = [\n{extra}\n]\n"
    # Idempotent: already injected for this project.
    window = rules_toml[idx : idx + 1200]
    if '"PY101"' in window and "WriterAgent project disable" in window:
        return rules_toml
    insert_at = idx + len(marker)
    lines = "".join(f'\n  "{rid}",  # WriterAgent project disable' for rid in _DISABLED_RULE_IDS)
    return rules_toml[:insert_at] + lines + rules_toml[insert_at:]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    from pyspector import ast_cache as pyspector_ast_cache
    from pyspector import cli as pyspector_cli
    from pyspector import config as pyspector_config
    try:
        from scripts.pyspector_semgrep_adapter import get_converted_semgrep_rules
    except ImportError:
        from pyspector_semgrep_adapter import get_converted_semgrep_rules

    original = pyspector_config.get_default_rules

    def patched_get_default_rules(ai_scan: bool = False) -> str:
        base_rules = _inject_disabled_rules(original(ai_scan))
        semgrep_rules = get_converted_semgrep_rules()
        return f"{base_rules}\n\n{semgrep_rules}" if semgrep_rules else base_rules

    pyspector_config.get_default_rules = patched_get_default_rules
    pyspector_cli.get_default_rules = patched_get_default_rules

    # cli imports get_cache by name; patch both the module and the CLI binding.
    def redirected_get_cache(
        scan_path: Optional[Path] = None,
    ) -> pyspector_ast_cache.IncrementalAstCache:
        # scan_path ignored: keep AST JSON under system temp, not plugin/.pyspector_cache.
        if pyspector_ast_cache._instance is None:
            pyspector_ast_cache._instance = pyspector_ast_cache.IncrementalAstCache(
                cache_dir=_AST_CACHE_DIR
            )
        return pyspector_ast_cache._instance

    pyspector_ast_cache.get_cache = redirected_get_cache
    pyspector_cli.get_cache = redirected_get_cache

    if not argv:
        argv = ["scan", "plugin", "--ai", "-c", "pyspector.toml", "--msg=False"]

    # PySpector 0.2.1 prints U+1F4A1 in _print_banner. Windows CI defaults to
    # cp1252, so click.echo raises UnicodeEncodeError before the scan starts.
    _configure_utf8_stdio()

    try:
        pyspector_cli.cli.main(args=argv, prog_name="pyspector")
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (1 if code else 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
