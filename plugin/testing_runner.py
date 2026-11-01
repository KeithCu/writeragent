#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Mini in-process test runner (no pytest dependency).
#
# This module can be called from:
# - Inside LibreOffice (given a UNO ComponentContext)
# - Outside LibreOffice via headless/user-profile Popen + UNO pipe (not
#   officehelper.bootstrap(soffice=<cmd string>) — current LO treats soffice=
#   as a single executable path)
#
# It aggregates existing in-LO tests (Writer/Calc, etc.) and returns
# a JSON summary that external tools or agents can consume.
#
# URP DisposedException on the next factory open: FAIL lines include
# previous=<last TEST end> (see format_lifecycle_breadcrumb). Soak:
# --repeat N / make test-uno-soak. SalAbort empty-text
# ("Unspecified Application Error") fail-closes the OK test.
# After a clean summary, ``-m`` uses os._exit(0) so pyuno GC SIGABRT
# cannot fail make test-uno (GHA 35527291175).
# docs/framework/uno-test-lifecycle.md

import argparse
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Sequence

log = logging.getLogger(__name__)

# CLI path/name filters from ``python -m plugin.testing_runner <filter>``.
# File-path filters select modules; leftover ``test_*`` names select functions.
_cli_filters: list[str] = []


def _progress(msg: str) -> None:
    """Print a line immediately so soffice aborts still name the last test.

    Always on: which test (``TEST call`` / ``TEST returned``), FAIL / SKIP /
    end, and death attribution for the current test. Bootstrap, pid dumps,
    soak iters, and other non-test chatter go through ``_progress_verbose``.
    """
    print(msg, file=sys.stderr, flush=True)


def _progress_verbose(msg: str) -> None:
    """Bootstrap, recycle, soak, and pid dumps. Hidden unless ``WRITERAGENT_CI_DEBUG=1``."""
    if os.environ.get("WRITERAGENT_CI_DEBUG") == "1":
        _progress(msg)


# Every native test aborts after this many seconds (override with
# WRITERAGENT_UNO_TEST_TIMEOUT). Silent — do not print arm/disarm lines.
_NATIVE_TEST_TIMEOUT_SEC = 30


def _native_test_timeout_sec() -> int:
    raw = os.environ.get("WRITERAGENT_UNO_TEST_TIMEOUT")
    if raw:
        try:
            return max(5, int(raw))
        except ValueError:
            pass
    return _NATIVE_TEST_TIMEOUT_SEC


def _arm_native_test_watchdog() -> None:
    """Abort if a native test never returns. No stderr on arm or disarm."""
    seconds = _native_test_timeout_sec()
    try:
        import faulthandler

        faulthandler.enable(file=sys.stderr, all_threads=True)
        faulthandler.dump_traceback_later(
            seconds,
            repeat=False,
            exit=True,
            file=sys.stderr,
        )
    except Exception:
        log.debug("native test watchdog arm failed", exc_info=True)


def _disarm_native_test_watchdog() -> None:
    import faulthandler

    faulthandler.cancel_dump_traceback_later()


def _soffice_pids_win32() -> str:
    """Parse ``tasklist`` CSV. ``pgrep`` is absent on GHA windows-latest
    (33699746211 printed ``soffice.bin=-`` while leftover dump still had
    soffice.exe / soffice.bin).
    """
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            timeout=2,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return "-"
    pids: list[str] = []
    for line in out.splitlines():
        low = line.lower()
        if "soffice.bin" not in low and "soffice.exe" not in low:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        pid = parts[1].strip().strip('"')
        if pid.isdigit():
            pids.append(pid)
    return ",".join(dict.fromkeys(pids)) or "-"


def _soffice_pids() -> str:
    """Best-effort soffice PIDs for correlating aborts with this run.

    Linux uses ``soffice.bin``; macOS Homebrew/app bundles name the process
    ``soffice``. ``pgrep -x soffice.bin`` on Darwin is why CI 33453203864
    printed ``soffice.bin=-`` while ``lo-kill`` still found PID 18456.
    Windows GHA has no ``pgrep`` — use ``tasklist``.
    """
    if sys.platform == "win32":
        return _soffice_pids_win32()

    pids: list[str] = []
    for name in ("soffice.bin", "soffice"):
        try:
            out = subprocess.check_output(
                ["pgrep", "-x", name],
                timeout=2,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            log.debug("soffice pid probe %s failed", name, exc_info=True)
            continue
        pids.extend(token for token in out.split() if token)
    return ",".join(dict.fromkeys(pids)) or "-"


def _fail_reason(exc: BaseException) -> str:
    """One-line FAIL reason for GHA. suite_log JSON is lost if a later test hangs."""
    text = "%s: %s" % (type(exc).__name__, exc)
    text = text.replace("\n", " ")
    if len(text) > 400:
        return text[:397] + "..."
    return text


# Last completed native test + soffice PIDs. A DisposedException on the *next*
# factory open is often leftover from this test's close, not a bug in the victim.
# Reset at the start of run_all_tests (not per suite — first test of suite N+1
# may die because suite N's last close toasted URP).
_LC_INITIAL = {
    "last_qual": "",
    "last_result": "",
    "last_end_pids": "",
    "last_ok_qual": "",
    "last_end_mono": 0.0,
    "current_qual": "",
    "current_start_pids": "",
    "current_start_mono": 0.0,
    "current_bridge": "",
}
_lc = SimpleNamespace(**_LC_INITIAL)

# ``--repeat N`` / ``WRITERAGENT_UNO_SOAK``: re-run selected suites in one office.
_soak_repeat: int = 1

# Named Draw pairs for ``--pair`` / ``make test-uno-soak PAIR=…`` (same office).
_SOAK_PAIRS: Dict[str, List[str]] = {
    "tree-math": ["test_get_draw_tree", "test_insert_math_draw"],
    "dup-move": [
        "test_duplicate_slide_copies_shapes",
        "test_duplicate_rename_move_slide",
    ],
}

# ``--pair`` uses exact function names. Prefix match on ``test_get_draw_tree``
# also selected ``test_get_draw_tree_marks_blank_and_label_hint`` (QA soak).
_cli_exact_function_names: list[str] = []

# VCL SalAbort empty-text fallback (LibreOffice vcl/source/app/salplug.cxx):
# fprintf(stderr, "Unspecified Application Error\n") then abort()/_exit(1).
# Not a Draw assertion. The office is dying; URP death is the next symptom.
_APPLICATION_ERROR_MARKER = "Unspecified Application Error"
_OFFICE_STDERR_TAIL = 30
_soffice_proc: Any = None
_office_stderr_lock = threading.Lock()
_office_stderr_application_error = False
_office_stderr_tail: list[str] = []
# Set by Windows peer teardown / skipped close_doc; consumed after that
# suite so later suites get a fresh soffice (GHA 34544965319: second
# Writer close_doc hung before any Impress in the same office).
_recycle_office_after_suite = False


def reset_office_death_signals(*, clear_proc: bool = False) -> None:
    """Clear SalAbort / stderr flags. Does not drop the live soffice Popen unless asked."""
    global _soffice_proc, _office_stderr_application_error, _office_stderr_tail
    with _office_stderr_lock:
        _office_stderr_application_error = False
        _office_stderr_tail = []
    if clear_proc:
        _soffice_proc = None


def reset_lifecycle_breadcrumb() -> None:
    """Clear the previous/current TEST trail (start of a run or unit test)."""
    global _lc
    _lc = SimpleNamespace(**_LC_INITIAL)
    reset_office_death_signals()


def note_office_stderr_line(line: str, *, echo: bool = False) -> None:
    """Record one office/Python stderr line; set the SalAbort flag if the marker appears.

    ``echo=True`` reprints on the real stderr (soffice PIPE drain). The TEST-call
    tee already wrote the line, so it only marks.
    """
    global _office_stderr_application_error
    text = line.rstrip("\r\n")
    if not text:
        return
    if echo:
        print(text, file=sys.__stderr__, flush=True)
    with _office_stderr_lock:
        _office_stderr_tail.append(text)
        if len(_office_stderr_tail) > _OFFICE_STDERR_TAIL:
            del _office_stderr_tail[0 : len(_office_stderr_tail) - _OFFICE_STDERR_TAIL]
        if _APPLICATION_ERROR_MARKER in text:
            _office_stderr_application_error = True


def consume_application_error() -> bool:
    """Return-and-clear whether Unspecified Application Error was seen since last consume."""
    global _office_stderr_application_error
    with _office_stderr_lock:
        seen = _office_stderr_application_error
        _office_stderr_application_error = False
        return seen


def office_stderr_tail() -> list[str]:
    with _office_stderr_lock:
        return list(_office_stderr_tail)


def soffice_exit_code() -> int | None:
    """``Popen.poll()`` of the harness soffice, or None if still running / no child."""
    proc = _soffice_proc
    if proc is None:
        return None
    return proc.poll()


def _drain_soffice_stderr(stream: Any) -> None:
    try:
        for line in stream:
            note_office_stderr_line(line, echo=True)
    except Exception:
        log.debug("soffice stderr drain failed", exc_info=True)
    finally:
        try:
            stream.close()
        except Exception:
            log.debug("soffice stderr drain close failed", exc_info=True)


class _StderrMarkerTee:
    """Wrap ``sys.stderr`` during a TEST call so in-process SalAbort text is flagged."""

    _inner: Any

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def write(self, data: Any) -> int:
        if isinstance(data, bytes):
            text = data.decode("utf-8", "replace")
        else:
            text = data if isinstance(data, str) else str(data)
        if text and _APPLICATION_ERROR_MARKER in text:
            note_office_stderr_line(text, echo=False)
        return self._inner.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def collect_post_test_death(ctx: Any = None) -> str | None:
    """Fail-closed reason if the office aborted during this TEST, else None.

    QA: killer printed Unspecified Application Error and still returned OK;
    the next open saw Binary URP already disposed. getServiceManager can lag
    SalAbort. Treat the stderr marker or a child exit as URP death.
    """
    app_err = consume_application_error()
    exit_code = soffice_exit_code()
    bridge = probe_uno_bridge(ctx)
    if not app_err and exit_code is None and bridge != "disposed":
        return None
    pids = _soffice_pids()
    crumb = format_lifecycle_breadcrumb()
    tail = office_stderr_tail()
    tail_note = ""
    if tail:
        tail_note = " stderr_tail=%s" % " | ".join(tail[-5:])
    if app_err:
        return (
            "Binary URP bridge disposed during call; Unspecified Application Error "
            "on office stderr (VCL SalAbort) soffice_exit=%s pids=%s bridge=%s %s%s"
            % (
                "-" if exit_code is None else exit_code,
                pids,
                bridge,
                crumb,
                tail_note,
            )
        )
    if exit_code is not None:
        return (
            "Binary URP bridge disposed during call; soffice exited %s during TEST "
            "pids=%s bridge=%s %s%s"
            % (exit_code, pids, bridge, crumb, tail_note)
        )
    return (
        "Binary URP bridge disposed during call; office dead after "
        "TEST returned (teardown likely killed URP) %s"
        % crumb
    )


def probe_uno_bridge(ctx: Any = None) -> str:
    """Cheap URP liveness: ``alive``, ``disposed``, ``no_probe``, or ``error:Type``.

    Uses ``ctx.getServiceManager()`` only (same check as ``_ensure_live_ctx``).
    Unit-test ctx objects without that method are ``no_probe`` — not a fail.
    """
    if ctx is None:
        return "no_probe"
    getter = getattr(ctx, "getServiceManager", None)
    if getter is None or not callable(getter):
        return "no_probe"
    try:
        getter()
    except Exception as exc:
        if _is_uno_bridge_disposed(exc):
            return "disposed"
        return "error:%s" % type(exc).__name__
    return "alive"


def record_test_start(qual: str, ctx: Any = None) -> None:
    """Remember the test about to run, soffice PIDs, and a cheap bridge probe."""
    _lc.current_qual = qual
    _lc.current_start_pids = _lc.last_end_pids or _soffice_pids()
    _lc.current_start_mono = time.monotonic()
    _lc.current_bridge = probe_uno_bridge(ctx)


def record_test_end(qual: str, result: str) -> None:
    """Promote the current test to 'previous' after TEST end (OK / FAIL / SKIP)."""
    _lc.last_qual = qual
    _lc.last_result = result
    _lc.last_end_pids = _lc.current_start_pids or _soffice_pids()
    _lc.last_end_mono = time.monotonic()
    if result == "OK":
        _lc.last_ok_qual = qual
    _lc.current_qual = ""
    _lc.current_start_pids = ""
    _lc.current_start_mono = 0.0
    _lc.current_bridge = ""


def format_lifecycle_breadcrumb() -> str:
    """One line naming the previous TEST end, last OK, current test, PIDs, bridge.

    Intermittent URP ``DisposedException`` on ``loadComponentFromURL`` usually
    names the *victim* (next ``@with_native_doc`` open). This string names the
    last test that finished — the likely killer — plus last successful TEST end,
    elapsed ms since that end, whether soffice PIDs changed, and a cheap
    getServiceManager probe.
    """
    prev = _lc.last_qual or "-"
    prev_result = _lc.last_result or "-"
    prev_pids = _lc.last_end_pids or "-"
    last_ok = _lc.last_ok_qual or "-"
    current = _lc.current_qual or "-"
    start_pids = _lc.current_start_pids or "-"
    now_pids = _soffice_pids()
    dt_ms = "-"
    if _lc.last_end_mono and _lc.current_start_mono:
        dt_ms = str(int(round((_lc.current_start_mono - _lc.last_end_mono) * 1000)))
    pids_changed = "0"
    if start_pids not in ("", "-") and now_pids != start_pids:
        pids_changed = "1"
    elif prev_pids not in ("", "-") and now_pids != prev_pids:
        pids_changed = "1"
    bridge = _lc.current_bridge or "no_probe"
    return (
        "previous=%s result=%s end_pids=%s last_ok=%s dt_ms=%s "
        "current=%s start_pids=%s now_pids=%s pids_changed=%s bridge=%s"
        % (
            prev,
            prev_result,
            prev_pids,
            last_ok,
            dt_ms,
            current,
            start_pids,
            now_pids,
            pids_changed,
            bridge,
        )
    )


def expand_soak_pair(name: str) -> List[str]:
    """Return ``test_*`` names for a Draw soak pair alias, or empty if unknown."""
    return list(_SOAK_PAIRS.get(str(name).strip().lower(), ()))


def _fail_reason_with_lifecycle(exc: BaseException) -> str:
    """FAIL reason plus previous-test breadcrumb (never truncated off the crumb)."""
    return "%s | %s" % (_fail_reason(exc), format_lifecycle_breadcrumb())


def _is_case_id(token: str) -> bool:
    """True for packet case ids like ``f3a``, ``b1a``, ``e9`` (not a lone packet letter)."""
    if len(token) < 2 or not token[0].isalpha():
        return False
    i = 1
    if not token[1].isdigit():
        return False
    while i < len(token) and token[i].isdigit():
        i += 1
    if i == len(token):
        return True
    return i == len(token) - 1 and token[i].isalpha()


def _test_function_filters(filters: Sequence[str]) -> list[str]:
    """Return CLI tokens that select tests (packet letter, case id, or ``test_*``).

    Skips path / ``*_uno`` module tokens. Packet letters are single A–Z;
    case ids are ``f3a`` / ``e9`` / ``r1``-style; full names start with ``test_``.
    """
    names: list[str] = []
    for token in filters:
        if "/" in token or "\\" in token or token.endswith(".py"):
            continue
        if token.endswith("_uno"):
            continue
        if token.startswith("test_"):
            names.append(token)
        elif len(token) == 1 and token.isalpha():
            names.append(token)
        elif _is_case_id(token.lower()):
            names.append(token)
    return names


def _function_name_matches(name: str, filters: Sequence[str]) -> bool:
    """True if ``name`` matches any packet letter, case id, or ``test_*`` filter.

    Packet ``F`` matches ``test_f18_…`` / ``test_f3a_…`` but not ``test_foo``.
    Case ``f1`` matches ``test_f1_…`` but not ``test_f10_…``.
    Full ``test_*`` tokens match exact name or a prefix ending at ``_``.
    """
    for token in filters:
        t = token.strip()
        if not t:
            continue
        if len(t) == 1 and t.isalpha():
            prefix = f"test_{t.lower()}"
            if name.startswith(prefix) and len(name) > len(prefix) and name[len(prefix)].isdigit():
                return True
            continue
        if t.startswith("test_"):
            if name == t:
                return True
            if name.startswith(t) and len(name) > len(t) and name[len(t)] == "_":
                return True
            continue
        low = t.lower()
        if _is_case_id(low):
            if name == f"test_{low}" or name.startswith(f"test_{low}_"):
                return True
    return False


def _module_matches_filters(full_path: str, filters: Sequence[str]) -> bool:
    """True if this UNO file should load given CLI filters (path or test selector)."""
    if not filters:
        return True
    func_filters = _test_function_filters(filters)
    path_filters = [f for f in filters if f not in func_filters]
    if any(token in full_path for token in path_filters):
        return True
    if not func_filters:
        return False
    try:
        source = Path(full_path).read_text(encoding="utf-8")
    except OSError:
        return False
    for def_name in re.findall(r"(?m)^\s*def (test_\w*)", source):
        if _function_name_matches(def_name, func_filters):
            return True
    return False

# Flag to run UNO chart tests with visible window rather than hidden
show_window: bool = False
# Packet F+E+B mock-sidebar: visible soffice with the developer's real user profile.
use_user_profile: bool = False

# Only these modules run under ``--user-profile`` (and they are skipped otherwise).
_USER_PROFILE_ONLY_UNO = frozenset(
    {
        "test_mock_llm_sidebar_uno.py",
        "test_mock_llm_peer_sidebar_uno.py",
    }
)


def _parse_cli_args(argv: Sequence[str]) -> list[str]:
    """Split runner flags from suite filters. Sets ``show_window`` / ``use_user_profile``.

    Mirror ``--user-profile`` into ``WRITERAGENT_UNO_USER_PROFILE`` as well.

    ``--repeat N`` (or ``WRITERAGENT_UNO_SOAK``) re-runs selected suites in the
    same soffice process to stress native-doc open/close. ``--pair tree-math``
    / ``dup-move`` expands to the two Draw tests that historically sandwich a
    URP death. See ``docs/framework/uno-test-lifecycle.md``.
    """
    global show_window, use_user_profile, _soak_repeat, _cli_exact_function_names
    _soak_repeat = 1
    _cli_exact_function_names = []

    parser = argparse.ArgumentParser(prog="plugin.testing_runner", allow_abbrev=False)
    parser.add_argument("--visible", "--show-window", action="store_true", dest="show_window")
    parser.add_argument("--user-profile", action="store_true")
    parser.add_argument("--repeat", type=int, default=None)
    parser.add_argument("--pair", default=None)
    known, filters = parser.parse_known_args(list(argv))

    if known.show_window:
        show_window = True
    if known.user_profile:
        use_user_profile = True
        show_window = True
        os.environ["WRITERAGENT_UNO_USER_PROFILE"] = "1"
    if known.repeat is not None:
        _soak_repeat = max(1, known.repeat)
    else:
        raw = os.environ.get("WRITERAGENT_UNO_SOAK")
        if raw:
            try:
                _soak_repeat = max(1, int(raw))
            except ValueError:
                pass
    if known.pair is not None:
        names = expand_soak_pair(known.pair)
        if names:
            filters.extend(names)
            _cli_exact_function_names = list(names)
        else:
            filters.append(str(known.pair))
    if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1":
        use_user_profile = True
        show_window = True
    return filters


# Headless throwaway UserInstallation created by ``_soffice_bootstrap_command``.
# Leftover geometric UNO writes Shared-kernel config here so soffice ``get_config``
# and the URP client are not looking at different ``writeragent.json`` files.
_throwaway_profile_dir: Path | None = None


def on_github_actions() -> bool:
    """True inside GitHub Actions (``GITHUB_ACTIONS=true``)."""
    return os.environ.get("GITHUB_ACTIONS") == "true"


def throwaway_writeragent_json() -> Path | None:
    """``user/config/writeragent.json`` under the headless throwaway profile, if any."""
    if _throwaway_profile_dir is None:
        return None
    return _throwaway_profile_dir / "user" / "config" / "writeragent.json"


def _seed_throwaway_profile_with_user_oxt(profile_dir: Path) -> None:
    """Copy user-level WriterAgent ``uno_packages`` into the throwaway profile.

    ``-env:UserInstallation=<tmp>`` does not inherit ``unopkg add`` on the
    real profile, so sheet ``=PY()`` is ``#NAME?``. ``unopkg add`` into the
    throwaway fails (helper soffice ``NoConnectException``). Copying
    ``user/uno_packages`` is what registers the components.
    """
    root = _libreoffice_user_lock_path().parent / "user" / "uno_packages"
    packages = root / "cache" / "uno_packages"
    found = False
    if packages.is_dir():
        try:
            for child in packages.iterdir():
                if child.name.endswith(".tmp_") and (child / "WriterAgent.oxt").exists():
                    found = True
                    break
        except OSError:
            pass
    if not found:
        _progress_verbose(
            "BOOTSTRAP throwaway seed skip — no user-level WriterAgent.oxt under %s "
            "(user unopkg is invisible to throwaway UserInstallation; "
            "sheet =PY() will be #NAME? 504/525) GITHUB_ACTIONS=%s"
            % (root, on_github_actions())
        )
        if on_github_actions():
            raise RuntimeError(
                "GitHub Actions must seed the UNO throwaway profile from the "
                "user-level WriterAgent install (make register-built-oxt). "
                "Missing %s" % root
            )
        return
    dest = profile_dir / "user" / "uno_packages"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(root, dest)
    _progress_verbose(
        "BOOTSTRAP throwaway seeded uno_packages from %s -> %s GITHUB_ACTIONS=%s"
        % (root, dest, on_github_actions())
    )
    # Shared-kernel leftover UNO needs soffice ``get_config`` to see ``shared``
    # on first read (2s mtime cache + Isolated OnNew otherwise). Isolated
    # tests that care must write Isolated after bootstrap.
    # Checkout ``.venv`` as scripting.python_venv_path made leftover Shared look
    # Isolated (x_geo_live undefined) on Linux and macOS. Sheet =PY() is
    # off-main; workers keep the office interpreter via resolve_libreoffice_python.
    cfg = profile_dir / "user" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    seeded: dict[str, str] = {"scripting.python_session_mode": "shared"}
    cfg_path = cfg / "writeragent.json"
    cfg_path.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")
    _progress_verbose(
        "BOOTSTRAP throwaway writeragent.json session_mode=shared venv=(not set) path=%s"
        % cfg_path
    )


def _soffice_bootstrap_command() -> tuple[list[str], str]:
    """Headless soffice argv. ``argv[0]`` is the executable path, not a shell string.

    Do not pass this to ``officehelper.bootstrap(soffice=...)``: current
    LibreOffice treats ``soffice=`` as one path and ``Popen``s it with no
    shell. Resolve the binary with ``_resolve_soffice_bin`` (macOS keeps
    ``officehelper.py`` in ``Contents/Resources`` and ``soffice`` in
    ``Contents/MacOS``). Windows must find ``soffice.exe``.
    ``--user-profile`` uses ``_user_profile_soffice_argv`` instead.
    """
    soffice = _resolve_soffice_bin()
    profile_dir = Path(tempfile.mkdtemp(prefix="writeragent-lo-test-profile-"))
    # Seed after the lookup so a missing user OXT on GitHub Actions
    # raises instead of becoming a silent None command.
    global _throwaway_profile_dir
    _throwaway_profile_dir = profile_dir
    _seed_throwaway_profile_with_user_oxt(profile_dir)
    profile_url = profile_dir.as_uri()
    # Same pipe name shape as officehelper / tools_lo; we own --accept now.
    pipe_name = "uno" + secrets.token_hex(8)
    accept = "pipe,name=%s;urp;" % pipe_name
    return [
        str(soffice),
        "--headless",
        "--nologo",
        "--nodefault",
        "--norestore",
        "--nofirststartwizard",
        "--nocrashreport",
        "-env:UserInstallation=%s" % profile_url,
        "--accept=%s" % accept,
    ], accept


# Inherited checkout / venv env makes soffice load mixed plugin sources; URP
# then reports the bridge disposed (testing_runner._bootstrap_office).
# __PYVENV_LAUNCHER__ is the Darwin venv leak into a child interpreter.
_SOFFICE_STRIP_ENV = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")

# Set when a URP dispose is seen so run_all_tests stops instead of 200+ dead suites.
_urp_bridge_dead: bool = False


def _child_env_without_runner_python(*, uno_thread_guard: bool | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in _SOFFICE_STRIP_ENV:
        env.pop(key, None)
    # URP dispatch of WriterAgentDeck runs getRealInterface off the VCL thread.
    # Product ChatPanel hops path init / get_extension_url via execute_on_main_thread.
    # WRITERAGENT_TESTING=1 still inlines that hop, so this child keeps the official
    # opt-out: Dummy-N create would otherwise abort under @main_thread_only.
    if uno_thread_guard is False:
        env["WRITERAGENT_UNO_THREAD_GUARD"] = "0"
    return env


def _user_profile_soffice_argv(soffice: Path, accept: str) -> list[str]:
    """Visible Writer on the real user profile. No ``--nodefault`` (officehelper crash).

    ``--norestore`` skips the crash-recovery dialog that otherwise blocks the UNO pipe.
    Tests then show ``WriterAgentDeck`` over UNO (View → Sidebar may be off).
    """
    return [
        str(soffice),
        "--norestore",
        "--nofirststartwizard",
        "--nocrashreport",
        "--nologo",
        "--writer",
        "--accept=%s" % accept,
    ]


def _libreoffice_user_lock_path() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", "")) / "LibreOffice" / "4"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "LibreOffice" / "4"
    else:
        base = Path.home() / ".config" / "libreoffice" / "4"
    return base / ".lock"


def _soffice_bin_running() -> bool:
    return _soffice_pids() != "-"


def _clear_stale_user_profile_ipc() -> None:
    """Drop leftover SingleOfficeIPC pipes and ``.lock`` when soffice is not running.

    A stale UserInstallation ``.lock`` with ``IPCServer=false`` makes the next
    soffice skip ``--accept``, so Packet F cannot connect.
    """
    if _soffice_bin_running():
        return
    lock = _libreoffice_user_lock_path()
    try:
        if lock.is_file():
            lock.unlink()
    except OSError:
        pass
    if not hasattr(os, "getuid"):
        return
    import glob

    tmp = tempfile.gettempdir()
    for path in glob.glob(os.path.join(tmp, "OSL_PIPE_%s_*" % os.getuid())):
        try:
            os.unlink(path)
        except OSError:
            pass


def _unused_tcp_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_soffice_bin() -> Path:
    _ensure_libreoffice_python_path()
    import officehelper

    name = "soffice.exe" if sys.platform.startswith("win") else "soffice"
    helper_file = officehelper.__file__
    assert helper_file is not None
    next_to_helper = Path(helper_file).resolve().parent / name
    if next_to_helper.exists():
        return next_to_helper
    which = shutil.which(name)
    if which:
        return Path(which)
    for candidate in (
        Path("/usr/lib/libreoffice/program") / name,
        Path("/snap/libreoffice/current/lib/libreoffice/program") / name,
        Path("/Applications/LibreOffice.app/Contents/MacOS") / name,
    ):
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "soffice not found (PATH, officehelper dir, or common install paths)"
    )


def _uno_resolver_for_local_ctx() -> Any:
    import uno

    local = uno.getComponentContext()
    smgr = getattr(local, "getServiceManager", lambda: None)()
    if smgr is None:
        smgr = getattr(local, "ServiceManager", None)
    if smgr is None:
        raise RuntimeError("no ServiceManager on local UNO context")
    return smgr.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )


def _headless_connect_delays() -> tuple[float, ...]:
    """Connect poll budget for headless ``make test-uno``.

    Windows GHA sometimes needs longer than the Linux ~21.5s budget before the
    named pipe accepts (same SHA: connected in ~5s vs miss at ~22s).
    """
    if sys.platform.startswith("win"):
        # ~44.5s — enough for the slow-accept twin of the intermittent flake.
        return (0.5, 1, 1, 2, 2, 3, 5, 8, 10, 12)
    return (0.5, 1, 1, 2, 2, 3, 5, 7)


def _connect_uno_accept(
    proc: Any,
    accept: str,
    *,
    path_label: str,
    delays: tuple[float, ...] = (0.5, 1, 1, 2, 2, 3, 5, 8, 8),
) -> Any:
    """Resolve ``uno:<accept>StarOffice.ComponentContext`` while soffice stays up."""
    from com.sun.star.connection import NoConnectException

    resolver = _uno_resolver_for_local_ctx()
    url = "uno:%sStarOffice.ComponentContext" % accept
    last_exc: BaseException | None = None
    t0 = time.monotonic()
    n = len(delays)
    for i, delay in enumerate(delays, start=1):
        time.sleep(delay)
        elapsed = time.monotonic() - t0
        code = proc.poll()
        if code is not None:
            raise RuntimeError(
                "%s soffice exited %s before UNO connect (crash recovery or mixed PYTHONPATH?)"
                % (path_label, code)
            )
        try:
            ctx = resolver.resolve(url)
            _progress_verbose(
                "BOOTSTRAP path=%s connected=True attempt=%s/%s elapsed=%.1fs soffice_exit=%s pids=%s"
                % (path_label, i, n, elapsed, proc.poll(), _soffice_pids())
            )
            return ctx
        except NoConnectException as exc:
            last_exc = exc
            _progress_verbose(
                "BOOTSTRAP path=%s attempt=%s/%s elapsed=%.1fs no_connect=%s pids=%s"
                % (path_label, i, n, elapsed, exc, _soffice_pids())
            )
    tail = office_stderr_tail()
    _progress_verbose(
        "BOOTSTRAP path=%s connected=False soffice_exit=%s pids=%s last=%s stderr_tail=%r"
        % (path_label, proc.poll(), _soffice_pids(), last_exc, tail[-30:])
    )
    raise RuntimeError("could not connect to %s soffice: %s" % (path_label, last_exc))


def _spawn_soffice(cmd: list[str], env: dict[str, str]) -> Any:
    global _soffice_proc
    proc = subprocess.Popen(
        cmd,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    _soffice_proc = proc
    from plugin.framework.worker_pool import run_in_background

    run_in_background(
        _drain_soffice_stderr, proc.stderr, name="soffice-stderr-drain", dedicated=True
    )
    return proc


def _bootstrap_user_profile_gui() -> Any:
    """Visible soffice like ``make lo-start``: user profile, ``--norestore --writer``, UNO pipe.

    ``officehelper.bootstrap`` always appends ``--nodefault --nologo --accept=pipe``.
    That path opened a window and then crashed (URP disposed). This start matches
    ``scripts/launch-lo-debug.sh`` plus an accept string so tests can attach.
    """
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("user-profile sidebar tests need DISPLAY (visible Writer)")
    soffice = _resolve_soffice_bin()
    _clear_stale_user_profile_ipc()
    port = _unused_tcp_port()
    accept = "socket,host=127.0.0.1,port=%s;urp;" % port
    cmd = _user_profile_soffice_argv(soffice, accept)
    import officehelper

    child_env = _child_env_without_runner_python(uno_thread_guard=False)
    _progress_verbose(
        "BOOTSTRAP path=user-profile stripped=%s officehelper=%s soffice_cmd=%r"
        % (
            [key for key in _SOFFICE_STRIP_ENV if key in os.environ],
            getattr(officehelper, "__file__", "?"),
            cmd,
        )
    )
    proc = _spawn_soffice(cmd, child_env)
    # GUI + extension OnStartApp is slower than headless.
    return _connect_uno_accept(proc, accept, path_label="user-profile")



def _terminate_bootstrap_soffice() -> None:
    """Best-effort kill of the harness soffice Popen before a bootstrap retry."""
    proc = _soffice_proc
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def request_office_recycle_after_suite() -> None:
    """Ask ``run_all_tests`` to kill+rebootstrap soffice after this suite."""
    global _recycle_office_after_suite
    _recycle_office_after_suite = True


def consume_office_recycle_request() -> bool:
    """Return-and-clear the after-suite recycle flag."""
    global _recycle_office_after_suite
    wanted = _recycle_office_after_suite
    _recycle_office_after_suite = False
    return wanted


# Windows: must run on the original soffice, before any suite-end recycle.
_WINDOWS_FIRST_IN_DIR = frozenset({"test_designs_uno.py"})


def _native_suite_sort_key(module_path: str) -> tuple[int, str]:
    """Windows-only order. POSIX returns ``(0, path)`` and stays path-sorted.

    Leftover docs must not sit in front of a later Hidden open. Band 2
    ``test_peer_message_uno.py`` is last. Band 1 is ``test_draw_uno.py``,
    ``test_formulas_uno.py``, and ``test_rich_html_uno.py``.
    ``test_designs_uno.py`` sorts first inside its directory so it runs on
    the original office, before any suite whose teardown asks for a recycle.
    History: ``docs/framework/uno-test-lifecycle.md``.
    """
    name = os.path.basename(module_path)
    if sys.platform != "win32":
        return (0, module_path)
    if name in _WINDOWS_FIRST_IN_DIR:
        # "\0" sorts before every sibling; the path prefix keeps the
        # suite inside its own directory's slot in band 0.
        return (0, module_path[: -len(name)] + "\0" + name)
    if name == "test_peer_message_uno.py":
        return (2, module_path)
    if name in (
        "test_draw_uno.py",
        "test_formulas_uno.py",
        "test_rich_html_uno.py",
    ):
        return (1, module_path)
    return (0, module_path)


def _rebootstrap_ctx(ctx: Any) -> None:
    """Bind config and the extension to a live UNO context.

    Recycle, the initial run_all_tests bootstrap, and _ensure_live_ctx
    all do this. Each caller keeps its own try/except.
    """
    from plugin.framework.uno_context import set_fallback_ctx
    from plugin.framework.config import init_config

    set_fallback_ctx(ctx)
    init_config(ctx)
    if not use_user_profile:
        from plugin.main import bootstrap

        bootstrap(ctx=ctx)


def _open_keeper(ctx: Any) -> Any:
    """Hidden Writer so the last closed test doc does not shut down soffice."""
    import uno
    from plugin.framework.uno_context import get_desktop
    from tests.testing_utils import set_harness_keeper_uid

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    doc = get_desktop(ctx).loadComponentFromURL(
        "private:factory/swriter", "_blank", 0, (hidden_prop,)
    )
    set_harness_keeper_uid(str(getattr(doc, "RuntimeUID", None) or ""), doc)
    return doc


def _recycle_harness_office(old_ctx: Any) -> tuple[Any, Any]:
    """Kill the current soffice and bootstrap a fresh one.

    Do **not** ``close`` leftover docs first — that is the hung path.
    Do **not** mark URP dead unless the new bootstrap fails.
    """
    _progress_verbose("LIFECYCLE recycle office after impress start")
    _terminate_bootstrap_soffice()
    time.sleep(0.5)
    reset_office_death_signals(clear_proc=True)
    _lc.last_end_pids = ""
    from tests.testing_utils import _NATIVE_DOC_POOL

    _NATIVE_DOC_POOL.clear()
    try:
        new_ctx = _bootstrap_office()
        if new_ctx is None:
            raise RuntimeError("recycle bootstrap returned None")
        _rebootstrap_ctx(new_ctx)
        new_keeper = None
        if not use_user_profile:
            new_keeper = _open_keeper(new_ctx)
        _progress_verbose(
            "LIFECYCLE recycle office after impress done pids=%s"
            % _soffice_pids()
        )
        return new_ctx, new_keeper
    except Exception as exc:
        _progress(
            "LIFECYCLE recycle office after impress failed err=%s:%s"
            % (type(exc).__name__, exc)
        )
        _mark_urp_dead(exc, "recycle office after impress")
        return old_ctx, None


def _bootstrap_office() -> Any:
    """Start soffice without the test runner's Python env in the child.

    A checkout ``PYTHONPATH`` makes the extension import mixed sources and
    the URP bridge comes up disposed. Headless uses argv ``Popen``. Do not
    call ``officehelper.bootstrap(soffice=<command string>)``: current
    LibreOffice treats ``soffice=`` as a path only.
    """
    if use_user_profile:
        return _bootstrap_user_profile_gui()
    import officehelper

    child_env = _child_env_without_runner_python()
    # Windows GHA: intermittent pipe miss while soffice stays alive — longer
    # budget plus one full restart. Linux keeps the shorter single attempt.
    attempts = 2 if sys.platform.startswith("win") else 1
    for attempt in range(1, attempts + 1):
        cmd, accept = _soffice_bootstrap_command()
        if attempt == 1:
            _progress_verbose(
                "BOOTSTRAP path=headless stripped=%s officehelper=%s soffice_cmd=%r resolved_soffice=%s"
                % (
                    [key for key in _SOFFICE_STRIP_ENV if key in os.environ],
                    getattr(officehelper, "__file__", "?"),
                    cmd,
                    cmd[0],
                )
            )
        else:
            _progress_verbose(
                "BOOTSTRAP path=headless retry=%s/%s after pipe miss; new profile"
                % (attempt, attempts)
            )
        try:
            proc = _spawn_soffice(cmd, child_env)
            ctx = _connect_uno_accept(
                proc,
                accept,
                path_label="headless",
                delays=_headless_connect_delays(),
            )
            _progress_verbose(
                "BOOTSTRAP path=headless returned=%s attempt=%s/%s pids=%s leftover_PYTHONPATH=%s"
                % (
                    ctx is not None,
                    attempt,
                    attempts,
                    _soffice_pids(),
                    os.environ.get("PYTHONPATH"),
                )
            )
            return ctx
        except Exception as exc:
            _progress_verbose(
                "BOOTSTRAP path=headless error=%s:%s attempt=%s/%s pids=%s stderr_tail=%r"
                % (
                    type(exc).__name__,
                    exc,
                    attempt,
                    attempts,
                    _soffice_pids(),
                    office_stderr_tail()[-30:],
                )
            )
            _terminate_bootstrap_soffice()
            if attempt >= attempts:
                raise
            # Brief pause so Windows releases the named pipe / process handles.
            time.sleep(2.0)



def native_test(func: Any) -> Any:
    """Decorator to mark a function as a test in the native test runner.

    Note: pytest-based runs will automatically skip/ignore these via a hook
    in conftest.py to keep the 'skipped' count meaningful.
    """
    func._is_test = True
    return func


def setup(func: Any) -> Any:
    """Decorator to mark a function as the setup routine for a test module."""
    func._is_setup = True
    return func


def teardown(func: Any) -> Any:
    """Decorator to mark a function as the teardown routine for a test module."""
    func._is_teardown = True
    return func


def _is_uno_bridge_disposed(exc: BaseException) -> bool:
    return type(exc).__name__ == "DisposedException" or "Binary URP bridge" in str(exc)


def _mark_urp_dead(exc: BaseException, where: str) -> None:
    """Record a dead URP bridge and stop scheduling more native suites."""
    global _urp_bridge_dead
    _urp_bridge_dead = True
    _progress("ABORT: URP bridge disposed at %s: %s; remaining suites skipped" % (where, exc))
    _print_office_log_tail()


def _print_office_log_tail(max_lines: int = 40) -> None:
    """Echo the end of the throwaway profile's writeragent_debug.log.

    Tip: Windows soffice has no usable stderr, so this log is the only trace
    of what the extension was doing when the bridge died.
    """
    if _throwaway_profile_dir is None:
        return
    path = _throwaway_profile_dir / "user" / "config" / "writeragent_debug.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        _progress("OFFICE LOG none at %s" % path)
        return
    _progress("OFFICE LOG tail %s (%d of %d lines)" % (path, min(max_lines, len(lines)), len(lines)))
    for line in lines[-max_lines:]:
        _progress("OFFICE LOG | %s" % line)


def _call_with_ctx(fn: Any, ctx: Any) -> None:
    """Pass ctx only when that parameter exists. No-arg teardowns stay no-arg."""
    if "ctx" in inspect.signature(fn).parameters:
        fn(ctx=ctx)
    else:
        fn()


def run_module_suite(ctx: Any, module: Any, name: str) -> tuple[int, int, list[str]]:
    """Monolithic entry point for running a test module (legacy/menu support).
    Returns (passed, failed, log).
    """
    log.info(f"run_module_suite start: {name}")
    total_passed = 0
    total_failed = 0
    suite_log = []

    setup_func = None
    teardown_func = None
    test_funcs = []

    # Discover decorators, iterating over module dict to preserve insertion (definition) order
    for _unused, attr in module.__dict__.items():
        if callable(attr):
            # `MagicMock` returns truthy values for any attribute access, so we must
            # check for an explicit boolean marker set by our decorators.
            if getattr(attr, "_is_setup", False) is True:
                setup_func = attr
            elif getattr(attr, "_is_teardown", False) is True:
                teardown_func = attr
            elif getattr(attr, "_is_test", False) is True:
                test_funcs.append(attr)

    # Discovery fallback: if no @test functions, check for old run_*_tests approach
    if not test_funcs:
        fallback_func_name = f"run_{name.split('.')[-1].replace('_tests', '').replace('test_', '')}_tests"
        # Bugfix: #2584 dropped this override, so "calc.tests" derived
        # "run_tests_tests", found nothing, and the Calc debug menu reported a
        # false-green 0 passed / 0 failed. The generic rule cannot map the bare
        # "tests" suffix to test_calc_uno.run_calc_tests, so name it explicitly.
        if name == "calc.tests":
            fallback_func_name = "run_calc_tests"
        fallback_func = getattr(module, fallback_func_name, None)
        if fallback_func:
            try:
                p, f, lines = fallback_func(ctx)
                return int(p or 0), int(f or 0), list(lines or [])
            except Exception as e:
                return 0, 1, [f"EXCEPTION in {fallback_func_name}: {e}", traceback.format_exc()]

    # Do not reset the lifecycle trail here: the first test of this suite may
    # fail on factory open because the *previous suite's last test* killed URP.
    _progress(f"SUITE start {name}")
    _progress_verbose(
        "SUITE pids %s python_pid=%s soffice.bin=%s" % (name, os.getpid(), _soffice_pids())
    )
    # GHA 34643210006: leftover HTML-paste Writers forced leftover Writer
    # reuse into notebook_runner. Isolate those suites on _wa_notebook_host.
    from tests.testing_utils import set_windows_notebook_host

    set_windows_notebook_host(
        name.endswith("test_notebook_runner_uno")
        or name.endswith("test_writer_importer_uno")
    )
    name_filters = _test_function_filters(_cli_filters)
    if _cli_exact_function_names:
        exact = set(_cli_exact_function_names)
        selected = [tf for tf in test_funcs if tf.__name__ in exact]
    elif name_filters:
        selected = [tf for tf in test_funcs if _function_name_matches(tf.__name__, name_filters)]
    else:
        selected = list(test_funcs)
    if (name_filters or _cli_exact_function_names) and not selected:
        msg = f"No tests matched filters {name_filters!r} in {name}"
        _progress(f"SUITE filter miss {name}: {name_filters}")
        set_windows_notebook_host(False)
        _progress(f"SUITE end {name} passed=0 failed=1")
        return 0, 1, [msg]
    if name_filters:
        _progress_verbose(f"SUITE selected {name}: {', '.join(tf.__name__ for tf in selected)}")

    def _fail_dead_office(qual: str, test_line: str, dead_exc: BaseException, lifecycle_msg: str) -> None:
        nonlocal total_failed
        total_failed += 1
        suite_log.append(f"{test_line} — FAIL ({dead_exc})")
        suite_log.append("LIFECYCLE %s" % format_lifecycle_breadcrumb())
        _progress(lifecycle_msg)
        _progress(f"TEST end {qual} FAIL {_fail_reason_with_lifecycle(dead_exc)}")
        _mark_urp_dead(dead_exc, qual)
        record_test_end(qual, "FAIL")

    try:
        if setup_func:
            _call_with_ctx(setup_func, ctx)

        for test_func in selected:
            test_line = f"Running test: {test_func.__name__}"
            qual = f"{name}.{test_func.__name__}"
            record_test_start(qual, ctx)
            _progress_verbose(
                "TEST start %s soffice=%s bridge=%s"
                % (qual, _lc.current_start_pids, _lc.current_bridge or "-")
            )
            # Per-test faulthandler abort (30s). Silent — TEST start/end is enough.
            _arm_native_test_watchdog()
            try:
                # SalAbort can print after the previous TEST end OK. Fail-closed
                # here so we do not open another Draw factory on a dying office.
                gap_death = collect_post_test_death(ctx)
                if gap_death:
                    dead_exc = RuntimeError(gap_death)
                    msg = (
                        "LIFECYCLE office death before TEST call %s soffice_exit=%s %s"
                        % (qual, soffice_exit_code(), format_lifecycle_breadcrumb())
                    )
                    _fail_dead_office(qual, test_line, dead_exc, msg)
                    break
                # GHA 33703959362: execute-done then 20min silence, no TEST end.
                # call vs returned splits body hang from post-return bookkeeping.
                _progress(f"TEST call {qual}")
                # In-process SalAbort text hits Python stderr; soffice PIPE drain
                # catches the same marker from the child (Popen inherits no longer).
                old_err = sys.stderr
                sys.stderr = _StderrMarkerTee(old_err)
                try:
                    _call_with_ctx(test_func, ctx)
                finally:
                    sys.stderr = old_err
                _progress(f"TEST returned {qual}")
                # Harness attribution (not a product fix): body + @with_native_doc
                # teardown returned, but SalAbort already printed, soffice exited,
                # or getServiceManager is disposed. Name *this* test as the killer.
                death = collect_post_test_death(ctx)
                if death:
                    dead_exc = RuntimeError(death)
                    if _APPLICATION_ERROR_MARKER in death:
                        msg = (
                            "LIFECYCLE application error after TEST returned %s soffice_exit=%s %s"
                            % (qual, soffice_exit_code(), format_lifecycle_breadcrumb())
                        )
                    else:
                        msg = (
                            "LIFECYCLE office dead after TEST returned %s %s"
                            % (qual, format_lifecycle_breadcrumb())
                        )
                    _fail_dead_office(qual, test_line, dead_exc, msg)
                    break
                total_passed += 1
                suite_log.append(f"{test_line} — OK")
                record_test_end(qual, "OK")
                exit_code = soffice_exit_code()
                _progress(f"TEST end {qual} OK")
                _progress_verbose(
                    "TEST end detail %s soffice=%s exit=%s"
                    % (qual, _lc.last_end_pids, "-" if exit_code is None else exit_code)
                )
            except ModuleNotFoundError as e:
                # Some "native" tests attempt to use pytest.skip, but LibreOffice's
                # Python may not have pytest installed.
                if getattr(e, "name", None) == "pytest":
                    suite_log.append(f"{test_line} — SKIP (pytest not available)")
                    record_test_end(qual, "SKIP")
                    _progress(f"TEST end {qual} SKIP")
                    continue
                total_failed += 1
                suite_log.append(f"{test_line} — FAIL (ModuleNotFoundError: {e})")
                suite_log.append(traceback.format_exc())
                _progress(f"TEST end {qual} FAIL {_fail_reason_with_lifecycle(e)}")
                record_test_end(qual, "FAIL")
            except unittest.SkipTest as e:
                total_passed += 1
                suite_log.append(f"{test_line} — OK (skipped) ({e})")
                record_test_end(qual, "SKIP")
                _progress(f"TEST end {qual} SKIP")
            except Exception as e:
                total_failed += 1
                crumb = format_lifecycle_breadcrumb()
                suite_log.append(f"{test_line} — FAIL ({type(e).__name__}: {e})")
                suite_log.append(f"LIFECYCLE {crumb}")
                suite_log.append(traceback.format_exc())
                _progress(f"TEST end {qual} FAIL {_fail_reason_with_lifecycle(e)}")
                record_test_end(qual, "FAIL")
                if _is_uno_bridge_disposed(e):
                    _progress("LIFECYCLE URP dispose at %s %s" % (qual, crumb))
                    _mark_urp_dead(e, qual)
                    break
            finally:
                _disarm_native_test_watchdog()

    except Exception as e:
        total_failed += 1
        suite_log.append(f"SUITE ABORTED EXCEPTION: {e}")
        suite_log.append(traceback.format_exc())
    finally:
        if teardown_func:
            try:
                _call_with_ctx(teardown_func, ctx)
            except Exception as e:
                if _is_uno_bridge_disposed(e):
                    suite_log.append(f"TEARDOWN SKIPPED: UNO bridge disposed ({e})")
                else:
                    total_failed += 1
                    suite_log.append(f"TEARDOWN EXCEPTION: {e}")
                    suite_log.append(traceback.format_exc())

    set_windows_notebook_host(False)
    _progress(f"SUITE end {name} passed={total_passed} failed={total_failed}")
    return total_passed, total_failed, suite_log


def _native_suite_set_config(
    key: str,
    value: Any,
    *,
    event_key: str | None,
    ctx: Any,
    review_mode_override: Dict[str, Any],
    original_set_config: Callable[..., None],
) -> None:
    """Stand-in for ``set_config`` while native suites run.

    Forward ``event_key``. ``set_text_model``, ``set_image_model``, and
    ``set_api_key_for_endpoint`` always pass it: the listener key when a
    settings field is stored under another name, or ``None`` so listeners
    see the stored key. A wrapper that only takes ``(key, value)`` raises
    ``TypeError`` and aborts both suites before any test
    (GHA 36809189426, 36809192143).

    ``doc.agent_edit_review_mode`` stays in memory; a caller-supplied
    ``event_key`` is still the ``config:changed`` key so that signal is
    not dropped.
    """
    if key == "doc.agent_edit_review_mode":
        review_mode_override[key] = value
        from plugin.framework.event_bus import get_event_bus

        bus = get_event_bus()
        if event_key:
            bus.emit("config:changed", key=event_key, value=value, ctx=ctx)
        else:
            bus.emit("config:changed", ctx=ctx)
        return
    original_set_config(key, value, event_key=event_key)


def run_all_tests(ctx: Any) -> str:
    """Run all in-process WriterAgent tests and return a JSON summary string.

    The JSON structure is:
        {
          "total_passed": int,
          "total_failed": int,  # omitted when zero
          "suites": [
            {
              "name": "writer.format_tests",
              "failed": int,  # omitted when zero
              "log": ["Running test: foo — OK", "Running test: bar — FAIL (...)", ...]
            },
            ...
          ]
        }

    This is intentionally minimal and self-contained so we don't need pytest
    inside LibreOffice. External callers can parse this JSON, print a report,
    and use total_failed as an exit code condition.
    """
    global _urp_bridge_dead
    _urp_bridge_dead = False
    reset_lifecycle_breadcrumb()
    # Mock doc.agent_edit_review_mode during tests to default to "off"
    # and only track its test-specific overrides in memory.
    import plugin.framework.config
    original_get_config = plugin.framework.config.get_config
    original_set_config = plugin.framework.config.set_config
    original_get_config_dict = plugin.framework.config.get_config_dict

    _review_mode_override: Dict[str, Any] = {}

    def test_get_config(key: str) -> Any:
        if key == "doc.agent_edit_review_mode":
            return _review_mode_override.get(key, "off")
        return original_get_config(key)

    def test_set_config(key: str, value: Any, *, event_key: str | None = None) -> None:
        _native_suite_set_config(
            key,
            value,
            event_key=event_key,
            ctx=ctx,
            review_mode_override=_review_mode_override,
            original_set_config=original_set_config,
        )

    def test_get_config_dict() -> dict[str, Any]:
        base = original_get_config_dict()
        merged = dict(base)
        merged["doc.agent_edit_review_mode"] = _review_mode_override.get("doc.agent_edit_review_mode", "off")
        return merged

    setattr(plugin.framework.config, "get_config", test_get_config)
    setattr(plugin.framework.config, "set_config", test_set_config)
    setattr(plugin.framework.config, "get_config_dict", test_get_config_dict)

    suites: List[Dict[str, Any]] = []


    total_passed = 0
    total_failed = 0


    keeper_doc = None

    if not use_user_profile:
        try:
            _progress_verbose(
                "KEEPER load start target=_blank hidden=True "
                "python_pid=%s soffice=%s" % (os.getpid(), _soffice_pids())
            )
            keeper_doc = _open_keeper(ctx)
            keeper_uid = str(getattr(keeper_doc, "RuntimeUID", None) or "-")
            _progress_verbose(
                "KEEPER load done ok=%s uid=%s python_pid=%s soffice=%s"
                % (keeper_doc is not None, keeper_uid, os.getpid(), _soffice_pids())
            )
        except Exception as e:
            log.warning("run_all_tests: could not create keeper document: %s", e)
            if _is_uno_bridge_disposed(e):
                _mark_urp_dead(e, "keeper document")
                return json.dumps(
                    {
                        "total_passed": 0,
                        "total_failed": 1,
                        "suites": [
                            {
                                "name": "bootstrap",
                                "failed": 1,
                                "log": [
                                    "could not create keeper document: %s" % e,
                                    "ABORT: URP disposed; not running remaining UNO suites",
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )

    # Initialize the tool registry (Writer/Calc/Draw modules) before loading any
    # UNO test file. Each suite below snapshots/restores sys.modules (uno, com,
    # …); if the first suite only pulled in a partial UNO graph, a later suite's
    # first ``get_tools()`` could otherwise see an empty registry or hit import
    # edge cases. Extension startup already sets ``_bootstrap_state``; this is a
    # no-op then.
    try:
        _rebootstrap_ctx(ctx)
    except Exception as e:
        log.warning("run_all_tests: bootstrap failed (in-LO tool tests may fail): %s", e)

    def _ensure_live_ctx(current_ctx: Any) -> Any:
        try:
            current_ctx.getServiceManager()
            return current_ctx
        except Exception as exc:
            if _is_uno_bridge_disposed(exc):
                _mark_urp_dead(exc, "_ensure_live_ctx getServiceManager")
                return current_ctx
        try:
            new_ctx = _bootstrap_office()
            _rebootstrap_ctx(new_ctx)
            return new_ctx
        except Exception as e:
            log.warning("run_all_tests: could not refresh disposed UNO context: %s", e)
            if _is_uno_bridge_disposed(e):
                _mark_urp_dead(e, "_ensure_live_ctx refresh")
            return current_ctx

    from plugin.framework.constants import get_plugin_dir
    import importlib.util

    tests_root = os.path.join(os.path.dirname(get_plugin_dir()), "tests")

    if os.path.isdir(tests_root):
        # Discover ``*_uno.py`` recursively under tests/ (collocated with the module).
        from tests.testing_utils import NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS

        _MISSING = object()

        # Gather all candidates
        test_candidates = []

        for root, _dirs, files in os.walk(tests_root):
            for filename in files:
                if not (filename.startswith("test_") and filename.endswith("_uno.py")):
                    continue
                user_only = filename in _USER_PROFILE_ONLY_UNO
                if user_only != use_user_profile:
                    continue
                full_path = os.path.join(root, filename)
                if _module_matches_filters(full_path, _cli_filters):
                    test_candidates.append(full_path)

        if not test_candidates:
            reason = (
                "No native test suites matched filters %r" % (_cli_filters,)
                if _cli_filters
                else "No native test suites found under %s" % tests_root
            )
            _progress("SUITE discovery miss: %s" % reason)
            suites.append({"name": "discovery", "failed": 1, "log": [reason]})
            total_failed += 1

        skip_keeper_close = False
        if _soak_repeat > 1:
            _progress_verbose(
                "SOAK start rounds=%s suites=%s filter=%s"
                % (_soak_repeat, len(test_candidates), _cli_filters or "-")
            )
        for soak_i in range(_soak_repeat):
            if _soak_repeat > 1:
                _progress_verbose("SOAK iter %s/%s" % (soak_i + 1, _soak_repeat))
            if _urp_bridge_dead:
                _progress("SOAK stop: URP already disposed")
                break
            ordered_candidates = sorted(test_candidates, key=_native_suite_sort_key)
            skip_keeper_close = False
            for i, module_path in enumerate(ordered_candidates):
                if _urp_bridge_dead:
                    _progress("SUITE skip remaining: URP already disposed")
                    break
                ctx = _ensure_live_ctx(ctx)
                if _urp_bridge_dead:
                    _progress("SUITE skip remaining: URP already disposed")
                    break
                filename = os.path.basename(module_path)
                
                # Construct a unique module name for sys.modules to avoid collisions
                # during the recursive walk.
                rel_path = os.path.relpath(module_path, tests_root)
                sys_module_name = "plugin.tests." + rel_path[:-3].replace(os.sep, ".")

                restore_snapshot: Dict[str, Any] = {k: sys.modules.get(k, _MISSING) for k in NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS}
                try:
                    spec = importlib.util.spec_from_file_location(sys_module_name, module_path)
                    assert spec is not None and spec.loader is not None
                    test_module = importlib.util.module_from_spec(spec)
                    sys.modules[sys_module_name] = test_module
                    spec.loader.exec_module(test_module)

                    # Menu-only facade (e.g. calc ``test_calc_uno``): aggregates other UNO
                    # modules via ``run_calc_tests`` / ``run_integration_tests`` and must not
                    # run here — ``'_uno.py' in filename`` matches it, but it has no
                    # ``@native_test`` and the generic fallback name would not map to those runners.
                    if getattr(test_module, "SKIP_NATIVE_RUN_ALL", False):
                        continue

                    suite_name = sys_module_name.replace("plugin.tests.", "")
                    p, f, suite_log = run_module_suite(ctx, test_module, suite_name)
                    entry: Dict[str, Any] = {"name": suite_name, "log": suite_log}
                    if f:
                        entry["failed"] = f
                    suites.append(entry)
                    total_passed += p
                    total_failed += f
                    if consume_office_recycle_request():
                        more = i + 1 < len(ordered_candidates)
                        if more:
                            ctx, keeper_doc = _recycle_harness_office(ctx)
                        else:
                            # GHA 34551644954: rebootstrap then hung the next
                            # scalc load. No remaining suites — just kill.
                            _progress_verbose(
                                "LIFECYCLE recycle office skipped; no remaining suites"
                            )
                            skip_keeper_close = True
                except Exception as e:
                    # A *_uno.py that fails to import (renamed symbol or
                    # syntax error) used to print and continue, so
                    # total_failed stayed 0 and main() exited 0.
                    # SKIP_NATIVE_RUN_ALL above is the only skip.
                    load_name = sys_module_name.replace("plugin.tests.", "")
                    message = "%s: %s" % (type(e).__name__, e)
                    print("Error loading %s: %s" % (filename, message), flush=True)
                    suites.append(
                        {
                            "name": load_name,
                            "failed": 1,
                            "log": [
                                "Error loading %s: %s" % (load_name, message),
                                traceback.format_exc(),
                            ],
                        }
                    )
                    total_failed += 1
                finally:
                    # Prevent sys.modules mocking from polluting later native tests.
                    for k, v in restore_snapshot.items():
                        if v is _MISSING:
                            sys.modules.pop(k, None)
                        else:
                            sys.modules[k] = v

        if skip_keeper_close:
            # Leftover peer docs / Impress: do not close(True). Kill soffice.
            _progress_verbose("LIFECYCLE terminate office after peer leftovers start")
            _terminate_bootstrap_soffice()
            _progress_verbose("LIFECYCLE terminate office after peer leftovers done")
            keeper_doc = None
    else:
        reason = "Tests directory not found: %s (tests not bundled?)" % tests_root
        _progress("SUITE discovery miss: %s" % reason)
        suites.append({"name": "discovery", "failed": 1, "log": [reason]})
        total_failed += 1

    if keeper_doc is not None:
        try:
            _progress_verbose("KEEPER close start")
            keeper_doc.close(True)
            _progress_verbose("KEEPER close done")
        except Exception:
            _progress("KEEPER close failed")

    summary: Dict[str, Any] = {"total_passed": total_passed, "suites": suites}
    if total_failed:
        summary["total_failed"] = total_failed
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _ensure_libreoffice_python_path() -> None:
    """Add standard LibreOffice installation directories to sys.path if not present.

    On macOS, ``officehelper.py`` / ``uno.py`` live in ``Contents/Resources``
    (LibreOffice ``LIBO_LIB_PYUNO_FOLDER``), not next to the framework
    ``python3`` that Makefile used to pick (CI 33708366478). Homebrew Caskroom
    copies of the same app bundle are included so a non-``/Applications``
    install still resolves.
    """
    candidates = [
        # Linux standard paths
        Path("/usr/lib/libreoffice/program"),
        Path("/usr/lib/python3/dist-packages"),
        Path("/usr/lib64/libreoffice/program"),
        Path("/opt/libreoffice/program"),
        # macOS standard paths (officehelper.py is here, not in the framework)
        Path("/Applications/LibreOffice.app/Contents/Resources"),
        Path("/Applications/LibreOffice.app/Contents/Frameworks"),
        Path("/Applications/LibreOffice.app/Contents/MacOS"),
        # Windows standard paths
        Path(os.environ.get("PROGRAMFILES", "C:\\Program Files") + "\\LibreOffice\\program"),
    ]
    for cask_root in (
        Path("/opt/homebrew/Caskroom/libreoffice"),
        Path("/usr/local/Caskroom/libreoffice"),
    ):
        if cask_root.is_dir():
            candidates.extend(cask_root.glob("*/LibreOffice.app/Contents/Resources"))
            candidates.extend(cask_root.glob("*/LibreOffice.app/Contents/Frameworks"))
            candidates.extend(cask_root.glob("*/LibreOffice.app/Contents/MacOS"))
    for p in candidates:
        if p.is_dir() and str(p) not in sys.path:
            sys.path.append(str(p))


def _alias_runner_module() -> None:
    """``python -m`` executes this file as ``__main__``; tests import the package name."""
    if __name__ == "__main__":
        sys.modules["plugin.testing_runner"] = sys.modules[__name__]


def main() -> int:
    """Command-line entrypoint: bootstrap LO and run tests.

    This lets you run tests from a normal shell without clicking menus::

        python -m plugin.testing_runner
        python -m plugin.testing_runner tests/chatbot/test_mock_llm_sidebar_uno.py E
        python -m plugin.testing_runner --user-profile …/test_mock_llm_sidebar_uno.py f3a
        python -m plugin.testing_runner --repeat 20 test_draw_uno
        python -m plugin.testing_runner --repeat 50 --pair tree-math

    Extra tokens select tests: packet letter (``B``/``C``/``D``/``E``/``F``/``G``/``P``/``K``/``T``/``R``), case id
    (``f3a`` / ``p1`` / ``k1``), or full ``test_*`` name. Prefer ``make test-mock-sidebar FILTER=P``
    for the dual-sidebar peer Packet.

    ``--repeat N`` (or ``WRITERAGENT_UNO_SOAK=N``) re-runs selected suites in the
    same soffice process to stress native-doc open/close. Draw subset::

        make test-uno-soak
        make test-uno-soak PAIR=tree-math REPEAT=50
        make test-uno-soak FILTER="test_get_draw_tree test_insert_math_draw" REPEAT=50

    See ``docs/framework/uno-test-lifecycle.md``.

    The import of officehelper/uno is done lazily so that this module
    can still be imported inside LibreOffice without pulling them in.
    """
    _ensure_libreoffice_python_path()
    try:
        import officehelper  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError as exc:
        # ImportError is also raised when officehelper.py is found but its
        # ``import uno`` fails (missing PYTHONPATH / URE_BOOTSTRAP on Darwin).
        print("ERROR: officehelper module is not available; run with LibreOffice's Python.", flush=True)
        print("  (%s: %s)" % (type(exc).__name__, exc), flush=True)
        return 1

    global _cli_filters
    _cli_filters = list(_parse_cli_args(sys.argv[1:]))

    # Suppress MCP server startup in the soffice child process; it inherits
    # this env var and McpModule.start_background() checks it.
    #
    # WRITERAGENT_TESTING only short-circuits QueueExecutor inline execution; it does
    # NOT disable Layer A (WRITERAGENT_UNO_THREAD_GUARD). Use make lo-test-threadguard
    # to run this suite with the viral UNO proxy so worker-thread violations fail loudly.
    os.environ["WRITERAGENT_TESTING"] = "1"

    try:
        ctx = _bootstrap_office()
    except Exception as e:
        # Hard fail: silent SKIP hid broken officehelper soffice= command strings
        # on rolling LO (CachyOS) while Ubuntu CI still used shell=True.
        print(
            "ERROR: LibreOffice UNO bootstrap failed; cannot run in-LO tests.\n"
            "  (%s: %s)" % (type(e).__name__, e),
            flush=True,
        )
        return 1

    if ctx is None:
        print("ERROR: Could not bootstrap LibreOffice (headless Popen/UNO connect returned None).", flush=True)
        return 1

    summary_json = run_all_tests(ctx)
    print(summary_json, flush=True)

    summary = json.loads(summary_json)

    # Force-close LibreOffice via the Makefile/caller instead of in-process to avoid hangs.

    # Print a compact "tail" summary so callers can scan results quickly
    # even when the output above includes verbose tracebacks/log spam.
    total_passed = int(summary.get("total_passed", 0) or 0)
    total_failed = int(summary.get("total_failed", 0) or 0)
    print(f'"total_passed": {total_passed},', flush=True)
    if total_failed:
        print(f'"total_failed": {total_failed},', flush=True)

    return 0 if total_failed == 0 else 1


def _exit_after_summary(code: int) -> None:
    """End ``python -m plugin.testing_runner`` after the suite summary.

    After a clean pass, normal interpreter shutdown runs pyuno / LibreOffice
    destructors. Those can SIGABRT (``FATAL: exception not rethrown`` /
    ``Fatal Python error: Aborted``) once every suite has already printed
    ``total_passed`` with zero failures. That turned a green Ubuntu
    ``test-uno`` into make Error 134 (GHA 35527291175, tip cc281f17).
    ``os._exit(0)`` skips atexit and GC of UNO proxies. The Makefile
    ``lo-kill`` still reaps soffice.

    A non-zero *code* still raises ``SystemExit`` so real test / bootstrap
    failures stay visible. Do not hard-exit 0 when the summary failed.
    """
    if code == 0:
        os._exit(0)
    raise SystemExit(code)


if __name__ == "__main__":
    _alias_runner_module()
    _exit_after_summary(main())
