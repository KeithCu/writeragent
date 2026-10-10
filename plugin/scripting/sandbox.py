# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side venv sandbox boundary: import whitelist, subprocess spawn env, interpreter resolution."""

from __future__ import annotations

import functools
import os
import sys
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Sequence

from plugin.framework.deal_shim import (
    DEAL_MAX_ARGV,
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_PATH,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    deal,
    inverse_ensure,
    ascii_bounded, str_bounded,
)

# --- Import whitelist (shared by venv_sandbox and import_policy) ---

# Dynamically sync/mirror allowed and dangerous modules from smolagents to avoid silent drift.
# Note: LibreHarper bundles sandbox.py for wrap_command_for_sandbox without bundling
# smolagents; fallback tuples ensure sandbox.py can still be imported in that slim package.
try:
    from plugin.contrib.smolagents.utils import BASE_BUILTIN_MODULES as _BASE_BUILTIN
    BASE_BUILTIN_MODULES: tuple[str, ...] = tuple(_BASE_BUILTIN)
except (ImportError, ModuleNotFoundError):
    BASE_BUILTIN_MODULES = (
        "collections",
        "datetime",
        "itertools",
        "math",
        "queue",
        "random",
        "re",
        "stat",
        "statistics",
        "time",
        "unicodedata",
    )

try:
    from plugin.contrib.smolagents.local_python_executor import DANGEROUS_MODULES as _DANGEROUS
    DANGEROUS_MODULES: tuple[str, ...] = tuple(_DANGEROUS)
except (ImportError, ModuleNotFoundError):
    DANGEROUS_MODULES = (
        "builtins",
        "io",
        "multiprocessing",
        "os",
        "pathlib",
        "pty",
        "shutil",
        "socket",
        "subprocess",
        "sys",
    )


def _writeragent_alias_mirrors(entries: tuple[str, ...]) -> tuple[str, ...]:
    """``writeragent.X`` spellings of plugin modules already on the allowlist.

    AliasImporter maps ``writeragent.X`` to ``plugin.X``. A blanket
    ``writeragent.*`` made that map every plugin module, so a sandboxed script
    could import ``plugin.framework.config`` and ``LlmClient``. The vendored
    checker cannot translate the alias, so each allowlisted ``plugin.`` entry
    is repeated under ``writeragent.``.
    """
    mirrors: list[str] = []
    for entry in entries:
        if entry.startswith("plugin."):
            mirrors.append("writeragent." + entry[len("plugin."):])
    return tuple(mirrors)


# Curated by WriterAgent (see docs/enabling_numpy_in_libreoffice.md)—not "whatever is in the venv".
# No ``writeragent.*``: that wildcard is not an allow. See ``import_authorized``.
# These libraries (pandas.*, numpy.*, PIL.*, matplotlib.*) can read/write files
# (e.g. DataFrame.to_csv, plt.savefig). That is accepted on purpose: compute callers
# are presumed trusted, and the container plus a scrubbed environment is the boundary.
# We do not want arbitrary writes as a goal; if callers ever become untrusted, revisit
# this allowlist (narrow to the submodules formulas need). Review noted 2026-10-06.
_VENV_STDLIB: tuple[str, ...] = (
    "copy",
    "csv",
    "dataclasses",
    "decimal",
    "enum",
    "fractions",
    "functools",
    "json",
    "operator",
    "platform",
    "pprint",
    "string",
    "textwrap",
    "typing",
)

_VENV_PACKAGES: tuple[str, ...] = (
    "numpy",
    "numpy.*",
    "pandas",
    "pandas.*",
    "scipy",
    "scipy.*",
    "sklearn",
    "sklearn.*",
    "matplotlib",
    "matplotlib.*",
    "seaborn",
    "seaborn.*",
    "sympy",
    "sympy.*",
    "statsmodels",
    "statsmodels.*",
    "networkx",
    "networkx.*",
    "PIL",
    "PIL.*",
    "data_profiling",
    "data_profiling.*",
    "pandas_montecarlo",
    "pandas_montecarlo.*",
    "cv2",
    # webview / PyQt / jedi are editor-only. They are probed in a one-shot
    # subprocess (venv_diagnostics), not imported into the warm =PY() worker.
    "writeragent",
    "plugin.scripting.writeragent_api",
    "plugin.scripting.writeragent_api.*",
    "plugin.scripting.writeragent_namespace",
    "plugin.scripting.writeragent_namespace.*",
    "plugin.scripting.payload_codec",
    "plugin.scripting.analysis",
    "css_inline",
    "latex2mathml",
    "latex2mathml.*",
    "plugin.scripting.viz",
    "plugin.scripting.symbolic",
    "plugin.scripting.units",
    "plugin.scripting.text_analytics",  # trusted text analytics (spaCy) for Run Python Script + direct imports in user scripts
    "spacy",
    "spacy.*",
    "textdescriptives",
    "spacytextblob",
    "spacytextblob.*",
    "pint",
    "pint.*",
    # duckdb stays importable so user scripts can call connect/execute/df on
    # the raw C module. Removing it from this allowlist made `import duckdb`
    # raise "Import of duckdb is not allowed" before get_safe_module could
    # return that module. session_duckdb() still returns
    # GuardedDuckDBConnection for trusted SQL. import_policy keeps the name
    # out of LLM blurbs.
    "duckdb",
    "duckdb.*",
    "sentence_transformers",
    "sentence_transformers.*",
    "transformers",
    "transformers.*",
    "yfinance",
    "yfinance.*",
    "pandas_ta",
    "pandas_ta.*",
    "quantstats",
    "quantstats.*",
    "pypfopt",
    "pypfopt.*",
    "plugin.scripting.quant",
    "plugin.scripting.optimize",
    "plugin.scripting.forecast",
    "plugin.scripting.calc_functions",
    "plugin.scripting.calc_functions.*",
)

_VENV_AUTHORIZED_IMPORT_BASE: tuple[str, ...] = _VENV_STDLIB + _VENV_PACKAGES

# Script imports whose plugin path stays off the direct list.
# vision: ``from writeragent.vision import run_vision`` (not plugin.vision.venv.vision).
# duckdb_sql: SQL templates. ``plugin.scripting.duckdb_sql`` stays unlisted so
# the LLM blurb and the import-policy test do not grow a direct plugin entry.
# ``duckdb`` / ``duckdb.*`` above are unchanged.
_ALIAS_ONLY_IMPORTS: tuple[str, ...] = (
    "writeragent.vision",
    "writeragent.scripting.duckdb_sql",
)

VENV_AUTHORIZED_IMPORTS: tuple[str, ...] = (
    _VENV_AUTHORIZED_IMPORT_BASE
    + _writeragent_alias_mirrors(_VENV_AUTHORIZED_IMPORT_BASE)
    + _ALIAS_ONLY_IMPORTS
)


def _alias_real_module(name: str) -> str | None:
    """Plugin module a ``writeragent`` alias import loads, else ``None``."""
    if name == "writeragent":
        return "plugin.scripting.writeragent_api"
    if name.startswith("writeragent."):
        return "plugin" + name[len("writeragent"):]
    return None


@functools.lru_cache(maxsize=128)
def _compile_allowlist(authorized_imports: tuple[str, ...]) -> tuple[frozenset[str], tuple[str, ...]]:
    exact: set[str] = set()
    wildcards: list[str] = []
    for entry in authorized_imports:
        if entry == "*":
            wildcards.append("")
        elif entry.endswith(".*"):
            prefix = entry[:-2]
            exact.add(prefix)
            wildcards.append(prefix + ".")
        else:
            exact.add(entry)
    return frozenset(exact), tuple(wildcards)


def _matches_allowlist(name: str, exact: frozenset[str], wildcards: tuple[str, ...]) -> bool:
    if name in exact:
        return True
    return any(name.startswith(p) for p in wildcards)


def import_authorized(name: str, authorized_imports: Sequence[str]) -> bool:
    """Whether *name* is on the sandbox import allowlist.

    An intermediate trie node such as 'plugin' or 'plugin.scripting' is not
    an authorized import. Require an exact allowlist entry or a wildcard
    prefix ('a.b.*' matching 'a.b' and 'a.b.<child>'). Precompute exact names
    and wildcard prefixes per allowlist instead of rebuilding the trie on
    every check.

    A ``writeragent.*`` import is allowed only when the plugin module
    AliasImporter would load is on the same list, or the alias itself is an
    explicit entry. The blanket pattern ``writeragent.*`` is ignored: it
    authorized every alias, and the hook then loaded ``plugin.framework.config``
    and ``LlmClient``. Names that are not aliases, including ``duckdb`` and
    ``duckdb.*``, are checked against the precomputed allowlist.
    """
    # crosshair: off
    tuple_imports = tuple(authorized_imports)
    exact, wildcards = _compile_allowlist(tuple_imports)

    real = _alias_real_module(name)
    if real is None:
        return _matches_allowlist(name, exact, wildcards)

    if _matches_allowlist(real, exact, wildcards):
        return True

    if "writeragent.*" in tuple_imports:
        without_blanket = tuple(item for item in tuple_imports if item != "writeragent.*")
        exact_no_blanket, wildcards_no_blanket = _compile_allowlist(without_blanket)
        return _matches_allowlist(name, exact_no_blanket, wildcards_no_blanket)

    return _matches_allowlist(name, exact, wildcards)


# In-process LO embedded sandbox (execute_python_script) — stdlib-only extras beyond BASE_BUILTIN_MODULES.
CALC_AUTHORIZED_IMPORTS: tuple[str, ...] = tuple(sorted(set(BASE_BUILTIN_MODULES) | {"json"}))

# --- Subprocess environment ---

_BLOCKED_ENV_TOKENS = frozenset({"KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL"})
# XAUTHORITY contains AUTH but is the X11 cookie path, not a credential name.
_ENV_CREDENTIAL_ALLOW = frozenset({"XAUTHORITY"})
# Token prefixes that are known non-credential device or system names (e.g. KEYBOARD_*).
_ENV_TOKEN_ALLOW = frozenset({"KEYBOARD"})

# LibreOffice sets PYTHONHOME/PYTHONPATH to its bundled stdlib; letting these
# leak into a venv subprocess causes SRE module mismatch and import failures.
# LD_PRELOAD, LD_AUDIT, and DYLD_INSERT_LIBRARIES run code before the harness.
# PYTHONSTARTUP, PYTHONUSERBASE, PYTHONBREAKPOINT, PYTHONINSPECT alter python execution.
# DATABASE_URL is a common connection string containing database credentials.
# WRITERAGENT_COMPUTE_WORKER is compute-service only; venv workers support bidirectional tool calls.
_BLOCKED_ENV_EXACT = {
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONBREAKPOINT",
    "PYTHONINSPECT",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DATABASE_URL",
    "WRITERAGENT_COMPUTE_WORKER",
}

_PIPE_BUF_TARGET = 1024 * 1024


# check-all 33668189572: scrub_subprocess_env ~6m under DEAL_MAX_ARGV=32; keep pytest wide.
_DEAL_SCRUB_DICT = 1 if UNDER_CROSSHAIR else DEAL_MAX_ARGV
_DEAL_SCRUB_KEY = 2 if UNDER_CROSSHAIR else DEAL_MAX_TOKEN
_DEAL_SCRUB_VAL = 4 if UNDER_CROSSHAIR else DEAL_MAX_ARGV


def _deal_scrub_env_ok_pytest(base: object) -> bool:
    # os.environ values (PATH) exceed DEAL_MAX_ARGV and names can exceed
    # a token. The old cap raised PreContractError before secrets were
    # dropped. Values must stay str so the post (str→str) holds.
    # CrossHair keeps the one-entry domain.
    if base is None:
        return True
    return isinstance(base, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in base.items())


def _deal_scrub_env_ok_crosshair(base: object) -> bool:
    return base is None or (
        isinstance(base, dict)
        and len(base) <= _DEAL_SCRUB_DICT
        and all(
            isinstance(k, str)
            and str_bounded(k, _DEAL_SCRUB_KEY)
            and isinstance(v, str)
            and str_bounded(v, _DEAL_SCRUB_VAL)
            for k, v in base.items()
        )
    )


_deal_scrub_env_ok = _deal_scrub_env_ok_crosshair if UNDER_CROSSHAIR else _deal_scrub_env_ok_pytest


def _deal_path_ok_pytest(path: object) -> bool:
    # Real venv and workspace paths are longer than DEAL_MAX_PATH (256).
    # The cap raised PreContractError instead of the body's bool/strip.
    return isinstance(path, str)


def _deal_path_ok_crosshair(path: object) -> bool:
    return str_bounded(path, DEAL_MAX_PATH)


_deal_path_ok = _deal_path_ok_crosshair if UNDER_CROSSHAIR else _deal_path_ok_pytest

# cover-all 35526755391: basename ~10m under str_bounded path. ASCII short under CrossHair.
_DEAL_BASENAME_LEN = 8 if UNDER_CROSSHAIR else DEAL_MAX_PATH


def _env_name_is_credential(name: str) -> bool:
    """True when an env name is a credential, matched on ``_`` tokens.

    Match tokens that equal, start with, or end with a blocked word.
    Prefix-only matching let OPENAI_APIKEY, GITHUB_APITOKEN, AWS_SECRETKEY,
    and DATABASE_URL/*_DSN through, and blocked KEYBOARD_*. KEYBOARD tokens
    are allowed. DATABASE_URL and names ending in _DSN are blocked.
    """
    nu = name.upper()
    if nu in _ENV_CREDENTIAL_ALLOW:
        return False
    if nu == "DATABASE_URL" or nu == "DSN" or nu.endswith("_DSN"):
        return True
    tokens = [part for part in nu.replace("-", "_").split("_") if part]
    for token in tokens:
        if any(token == allowed or token.startswith(allowed) for allowed in _ENV_TOKEN_ALLOW):
            continue
        for word in _BLOCKED_ENV_TOKENS:
            if (
                token == word
                or token.startswith(word)
                or token.endswith(word)
                or token.endswith(word + "S")
            ):
                return True
    return False


@deal.pre(lambda base: _deal_scrub_env_ok(base))
@deal.post(lambda result: isinstance(result, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in result.items()))
@inverse_ensure(lambda base, result: all(k.upper() not in _BLOCKED_ENV_EXACT for k in result))
@inverse_ensure(lambda base, result: all(not _env_name_is_credential(k) for k in result))
@deal.ensure(
    lambda base, result: base is None
    or (
        result.get("PYTHONIOENCODING") == "utf-8"
        and result.get("PYTHONUTF8") == "1"
        and result.get("PYTHONDONTWRITEBYTECODE") == "1"
    )
)
def scrub_subprocess_env(base: dict[str, str] | None) -> dict[str, str]:
    """Drop likely-secret vars and LO Python overrides from the environment passed to venv Python.

    Only ``base is None`` returns {}. An empty dict still receives the UTF-8
    and bytecode overrides. Returning {} for {} skipped those overrides.
    """
    if base is None:
        return {}
    out: dict[str, str] = {}
    for k, v in base.items():
        ku = k.upper()
        if ku in _BLOCKED_ENV_EXACT:
            continue
        if _env_name_is_credential(k):
            continue
        out[k] = v
    # setdefault kept whatever the parent already had. Callers pass
    # dict(os.environ) (venv worker, editor host, compute worker). A latin-1
    # PYTHONIOENCODING, PYTHONUTF8=0, or PYTHONDONTWRITEBYTECODE=0 then failed
    # the ensure in deal builds and spawned the child with that encoding once
    # release builds strip deal. Force the values the postcondition requires.
    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONUTF8"] = "1"
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    # Child processes should log to the same writeragent_debug.log. Read the
    # path at call time (logging may not be initialized at import). Use the
    # public getter; a private import plus bare ``except Exception`` hid
    # failures other than a missing logging module.
    try:
        from plugin.framework.logging import get_debug_log_path
    except ImportError:
        return out
    debug_log_path = get_debug_log_path()
    if debug_log_path:
        out["WRITERAGENT_DEBUG_LOG_PATH"] = debug_log_path
    return out


@functools.cache
def detect_sandbox() -> str | None:
    """Return ``'flatpak'``, ``'snap'``, or ``None``.

    The result is cached because sandbox status cannot change at runtime.
    """
    # crosshair: off
    if os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID"):
        return "flatpak"
    if os.environ.get("SNAP_NAME"):
        return "snap"
    return None


def optimize_pipe(pipe_fd: int) -> None:
    """Raise venv-worker pipe capacity toward 1 MiB on Linux (default ~64 KiB).

    Large pickle IPC (split-grid / NumPy) can exceed the default pipe buffer;
    F_SETPIPE_SZ requests a larger kernel ring buffer so host and child block less.
    No-op on macOS/Windows (no supported API). Silently no-ops when caps deny resize.
    """
    # crosshair: off
    if sys.platform != "linux":
        return
    import fcntl

    cmd = getattr(fcntl, "F_SETPIPE_SZ", None)
    if cmd is None:
        return
    try:
        fcntl.fcntl(pipe_fd, cmd, _PIPE_BUF_TARGET)
    except OSError:
        pass


def optimize_popen_pipes(proc: subprocess.Popen[Any]) -> None:
    """Apply :func:`optimize_pipe` to stdin/stdout/stderr of a piped child process."""
    # crosshair: off
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            optimize_pipe(stream.fileno())
        except (OSError, ValueError):
            pass


# Hypothesis and venv-path tests pass Unicode argv/paths; ascii_bounded would reject them.
# Argv uses DEAL_MAX_ARGV (venv -c probes are longer than DEAL_MAX_PATH filesystem caps).
@deal.pre(
    lambda cmd: isinstance(cmd, list)
    and len(cmd) <= DEAL_MAX_CMD_ARGS
    and all(str_bounded(x, DEAL_MAX_ARGV) for x in cmd)
)
@deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))
@deal.ensure(lambda cmd, result: len(result) >= len(cmd) and (result[-len(cmd):] == cmd if cmd else True))
def wrap_command_for_sandbox(cmd: list[str]) -> list[str]:
    """Prepend ``flatpak-spawn --host`` when running inside a Flatpak sandbox.

    Snap confinement with ``classic``/``home`` plugs typically allows direct
    subprocess access, so Snap commands are returned unchanged.
    """
    sandbox = detect_sandbox()
    if sandbox == "flatpak":
        return ["flatpak-spawn", "--host"] + cmd
    return cmd


def _reset_cache() -> None:  # pyright: ignore[reportUnusedFunction]  # test helper to clear sandbox path cache
    """Reset the cached detection result (for tests only)."""
    detect_sandbox.cache_clear()


# --- Interpreter resolution ---


@deal.pre(lambda path: _deal_path_ok(path))
def _strip_surrounding_quotes(path: str) -> str:
    """Strip one layer of matching quotes (Windows Explorer \"Copy as path\")."""
    # crosshair: off
    # strip/quote SMT leftover (check-all 33668189572: Prev 8:33 despite DEAL_MAX_PATH). Doable later: tiny quoted-path alphabet.
    s = path.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1].strip()
    return s


def _path_from_file_url(raw: str) -> str | None:
    """Convert a ``file://`` / ``file:/`` URL to a filesystem path (stdlib only)."""
    # crosshair: off
    # urlparse/unquote/url2pathname combinatorics on free strings (cover-all 33293627157: ~2.0h, 190k lines / 108k examples). Doable later with a tiny file-URL alphabet.
    from urllib.parse import unquote, urlparse

    text = raw.strip()
    if text.startswith("file:/") and not text.startswith("file://"):
        text = "file://" + text[len("file:") :]
    if not text.startswith("file://"):
        return None
    parsed = urlparse(text)
    if parsed.scheme != "file":
        return None
    unquoted = unquote(parsed.path)
    if os.name == "nt" and len(unquoted) >= 3 and unquoted[0] == "/" and unquoted[2] == ":":
        path = unquoted[1:].replace("/", "\\")
    else:
        path = unquoted
    # Windows: file://server/share → netloc=server, path=/share
    if parsed.netloc and os.name == "nt" and not path.startswith("\\\\"):
        # UNC: file://server/share → netloc=server, path=/share
        path = f"\\\\{parsed.netloc}{path}"
    return path or None


def _normalize_venv_path_input(venv_dir: str) -> str:
    """Strip quotes, convert file URLs, then expand ``~`` and env vars."""
    # crosshair: off
    # expandvars/file-URL path still combinatoric as an entry (cover-all 33293627157: ~4.5m, 139k lines). Doable later with DEAL_MAX_PATH + opaque URL helper.
    cleaned = _strip_surrounding_quotes(venv_dir.strip())
    if cleaned.lower().startswith("file:"):
        from_url = _path_from_file_url(cleaned)
        if from_url:
            cleaned = from_url
    return os.path.expanduser(os.path.expandvars(cleaned))


@deal.pre(lambda base: ascii_bounded(base, _DEAL_BASENAME_LEN))
def _is_acceptable_python_basename(base: str) -> bool:
    """True for python / python3 / python.exe; false for pythonw and -config scripts."""
    # python3.X-config scripts are shell wrappers, not Python interpreters.
    lower = base.lower()
    if lower in ("pythonw", "pythonw.exe") or lower.endswith(("-config", "-config.exe")):
        return False
    return lower.startswith("python")


def _is_usable_python_file(path: str) -> bool:
    # crosshair: off
    # cover-all 33689813185 leftover: os.path.isfile/access combinatorics. Doable later: closed path alphabet.
    """True when *path* is a usable console Python interpreter file."""
    if not os.path.isfile(path):
        return False
    if not _is_acceptable_python_basename(os.path.basename(path)):
        return False
    # Windows ignores the Unix execute bit; require isfile only there.
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def _python_beside_soffice(soffice_path: str) -> Optional[str]:
    # crosshair: off
    # cover-all 33689813185 leftover cluster: filesystem walk beside soffice. Doable later.
    """Office-bundled interpreter next to soffice (not checkout ``.venv``).

    Windows ``sys.executable`` is often ``soffice.exe``; Darwin is empty or
    ``Contents/MacOS/soffice``. Sibling / Resources python is the same
    interpreter Linux leftover Shared already uses via ``sys.executable``.
    Seeding checkout ``.venv`` as ``python_venv_path`` made A3 Isolated
    (GHA 33751116865 Linux, 33752809831 Mac).
    """
    program = os.path.dirname(os.path.abspath(soffice_path))
    if not program:
        return None
    names = ("python.exe", "python.bin", "python", "python3")
    for name in names:
        candidate = os.path.join(program, name)
        if _is_usable_python_file(candidate):
            return candidate
    # Darwin: Contents/MacOS/soffice → Contents/Resources/python (PR #561).
    resources = os.path.join(os.path.dirname(program), "Resources", "python")
    if _is_usable_python_file(resources):
        return resources
    return None


def _bundled_lo_python_candidates() -> list[str]:
    # crosshair: off
    # cover-all 33689813185 leftover cluster: os.listdir install-layout walk. Doable later.
    """Install-layout fallbacks when ``sys.executable`` is empty (Darwin soffice)."""
    out: list[str] = []
    if os.name == "nt":
        for root_key, default in (
            ("PROGRAMFILES", r"C:\Program Files"),
            ("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        ):
            root = os.environ.get(root_key, default)
            out.append(os.path.join(root, "LibreOffice", "program", "python.exe"))
        return out
    out.extend(
        (
            "/Applications/LibreOffice.app/Contents/Resources/python",
            "/usr/lib/libreoffice/program/python.bin",
            "/usr/lib/libreoffice/program/python",
        )
    )
    for cask_root in (
        "/opt/homebrew/Caskroom/libreoffice",
        "/usr/local/Caskroom/libreoffice",
    ):
        if not os.path.isdir(cask_root):
            continue
        try:
            versions = os.listdir(cask_root)
        except OSError:
            continue
        for version in versions:
            out.append(
                os.path.join(
                    cask_root,
                    version,
                    "LibreOffice.app",
                    "Contents",
                    "Resources",
                    "python",
                )
            )
    return out


def resolve_libreoffice_python() -> Optional[str]:
    """Return a usable office Python: ``sys.executable``, else bundled neighbor.

    Under PyUNO this is normally the office-bundled Python. On Windows/macOS
    ``sys.executable`` is often soffice or empty (GHA 33752806292 / 33749078050)
    — look next to that binary and at the install layouts before giving up.
    Callers still surface an error so the user can set a venv.
    """
    # crosshair: off
    exe = (getattr(sys, "executable", None) or "").strip()
    if exe and os.path.isfile(exe):
        if _is_usable_python_file(exe):
            return exe
        neighbor = _python_beside_soffice(exe)
        if neighbor:
            return neighbor
    for candidate in _bundled_lo_python_candidates():
        if _is_usable_python_file(candidate):
            return candidate
    return None


def _python_candidates_in_bin_dir(bin_dir: str) -> list[str]:
    # crosshair: off
    # cover-all 33689813185 leftover: os.listdir python3.* combinatorics (~313 ex). Doable later: closed bin names.
    """Return candidate interpreter paths under a venv ``bin/`` or ``Scripts/`` directory."""
    candidates: list[str] = []
    if os.name == "nt":
        candidates.extend(
            [
                os.path.join(bin_dir, "python.exe"),
                os.path.join(bin_dir, "python"),
                os.path.join(bin_dir, "python3"),
            ]
        )
    else:
        for name in ("python", "python3"):
            candidates.append(os.path.join(bin_dir, name))
    # os.listdir raises OSError on a race or an unreadable directory. Same
    # guard as _bundled_lo_python_candidates.
    if os.path.isdir(bin_dir):
        try:
            entries = sorted(os.listdir(bin_dir))
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith("python3."):
                candidates.append(os.path.join(bin_dir, entry))
    return candidates


def _python_candidates_at_env_root(env_dir: str) -> list[str]:
    # crosshair: off
    # cover-all 33689813185 leftover: env-root path combinatorics. Doable later.
    """Return interpreter candidates at the env root (conda / pyenv-win layout)."""
    if os.name == "nt":
        return [
            os.path.join(env_dir, "python.exe"),
            os.path.join(env_dir, "python"),
            os.path.join(env_dir, "python3"),
        ]
    return [
        os.path.join(env_dir, "python"),
        os.path.join(env_dir, "python3"),
    ]


def _first_executable_python(candidates: list[str]) -> str | None:
    # crosshair: off
    # cover-all 33689813185 leftover: isfile walk (~473 ex). Doable later: closed candidate list.
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_usable_python_file(candidate):
            return candidate
    return None


def resolve_venv_python(venv_dir: str) -> Optional[str]:
    """Return the python executable for *venv_dir*.

    Accepts a venv root (``…/myvenv``), ``bin/`` / ``Scripts/`` directory, or a direct
    path to ``python`` / ``python3`` / ``python.exe``. Also accepts conda/pyenv-win
    layouts with ``python.exe`` at the env root. Strips surrounding quotes and
    converts ``file://`` URLs from pasted paths.
    """
    # crosshair: off
    if not venv_dir or not venv_dir.strip():
        return None
    expanded = _normalize_venv_path_input(venv_dir)

    if os.path.isfile(expanded):
        if _is_usable_python_file(expanded):
            return expanded
        return None

    if not os.path.isdir(expanded):
        return None

    dir_name = os.path.basename(os.path.normpath(expanded))
    if dir_name in ("bin", "Scripts"):
        return _first_executable_python(_python_candidates_in_bin_dir(expanded))

    if os.name == "nt":
        bin_candidates = [os.path.join(expanded, "Scripts"), os.path.join(expanded, "bin")]
    else:
        bin_candidates = [os.path.join(expanded, "bin"), os.path.join(expanded, "Scripts")]
    candidates: list[str] = []
    for bin_dir in bin_candidates:
        if os.path.isdir(bin_dir):
            candidates.extend(_python_candidates_in_bin_dir(bin_dir))
    # Prefer bin/Scripts; fall back to env-root python.exe (conda / pyenv-win).
    candidates.extend(_python_candidates_at_env_root(expanded))
    return _first_executable_python(candidates)


@deal.pre(lambda target_path, root_dir: _deal_path_ok(target_path) and _deal_path_ok(root_dir))
@deal.post(lambda result: isinstance(result, bool))
@deal.ensure(
    lambda target_path, root_dir, result: (
        not result
        or os.path.commonpath(
            [
                os.path.realpath(os.path.join(os.path.realpath(root_dir), target_path)),
                os.path.realpath(root_dir),
            ]
        )
        == os.path.realpath(root_dir)
    )
)
def is_safe_workspace_path(target_path: str, root_dir: str) -> bool:
    """Return True if *target_path* resolves strictly inside *root_dir*.

    ``realpath`` follows symlinks. ``abspath`` only collapses ``..``, so a
    link inside the root that pointed outside used to count as safe.
    """
    if not target_path or not root_dir:
        return False
    try:
        # abspath left symlink targets unchecked: root/link -> /etc/passwd
        # stayed "inside" root because the link path itself was inside.
        # realpath resolves that target before the commonpath comparison.
        abs_root = os.path.realpath(root_dir)
        abs_target = os.path.realpath(os.path.join(abs_root, target_path))
        return os.path.commonpath([abs_target, abs_root]) == abs_root
    except Exception:
        return False