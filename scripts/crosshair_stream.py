#!/usr/bin/env python3
# WriterAgent — live formatter for CrossHair check / cover output
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Format CrossHair ``check`` and ``cover`` output for live terminal reading.

Pipe CrossHair ``-v`` through the filter (full module, no timeout)::

    crosshair check -v --report_all plugin/scripting/payload_codec.py 2>&1 \\
        | python scripts/crosshair_stream.py check

    crosshair cover -v plugin/scripting/payload_codec.py 2>&1 \\
        | python scripts/crosshair_stream.py cover

    make crosshair-check
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO

CHECK_LINE = re.compile(
    r"^(?P<file>.+\.py):(?P<line>\d+): (?P<level>error|info|warning): (?P<msg>.*)$"
)
COVER_EXAMPLE = re.compile(r"^[A-Za-z_][\w.]*\(")
TRACE_LINE = re.compile(r"^(Traceback \(most recent call last\)|  File |TypeError:|ValueError:|IndexError:|KeyError:|AttributeError:)")
TRACE_FILE = re.compile(r'^File "(?P<path>[^"]+\.py)", line (?P<line>\d+)')
CROSSHAIR_INTERNAL = re.compile(r"CrossHairInternal|crosshair\.util\.CrossHairInternal")
# CrossHair --verbose: "23222.229|    |analyze_function() Analyzing  foo"
VERBOSE_PREFIX = re.compile(r"^\d+\.\d+\|(?:\s*\|)*\s*")
VERBOSE_ANALYZE_FN = re.compile(r"analyze_function\(\)\s+Analyzing\s+(\S+)")
VERBOSE_ANALYZE_COND = re.compile(r"analyze\(\)\s+Analyzing (pre|post)condition:\s*(.+)", re.I)
VERBOSE_ANALYZE_CLASS = re.compile(r"analyze_class\(\)\s+Analyzing class\s+(\S+)")


@dataclass
class StreamStats:
    """Running counters while CrossHair streams."""

    confirmed: int = 0
    not_confirmed: int = 0
    unable: int = 0
    check_errors: int = 0
    progress: int = 0
    examples: int = 0
    explore: int = 0
    cover_errors: int = 0
    suppressed: int = 0
    lines: int = 0
    # Deduped human-readable failures for the end-of-run "ERRORS TO FIX" block.
    error_details: list[str] | None = None

    def __post_init__(self) -> None:
        if self.error_details is None:
            self.error_details = []

    def summary(self, mode: str) -> str:
        if mode == "check":
            return (
                f"confirmed={self.confirmed} not_confirmed={self.not_confirmed} "
                f"unable={self.unable} errors={self.check_errors} progress={self.progress}"
            )
        if mode == "cover":
            return f"examples={self.examples} explore={self.explore} errors={self.cover_errors}"
        return (
            f"check(confirmed={self.confirmed} not_confirmed={self.not_confirmed} "
            f"unable={self.unable} errors={self.check_errors}) "
            f"cover(examples={self.examples} explore={self.explore} errors={self.cover_errors})"
        )

    @property
    def failure_count(self) -> int:
        return self.check_errors + self.cover_errors

    def record_error(self, detail: str) -> None:
        """Keep unique error details for the final fix-up summary."""
        assert self.error_details is not None
        text = detail.strip()
        if not text or text in self.error_details:
            return
        self.error_details.append(text)


@dataclass
class ClassifiedLine:
    tag: str
    detail: str
    raw: str
    show_stats: bool = True


def _strip_crosshair_verbose(line: str) -> str:
    return VERBOSE_PREFIX.sub("", line.strip())


def _plugin_relpath(path: str) -> str | None:
    """Return path relative to ``plugin/`` when the frame is in our tree."""
    normalized = path.replace("\\", "/")
    marker = "/plugin/"
    idx = normalized.rfind(marker)
    if idx >= 0:
        return "plugin/" + normalized[idx + len(marker) :]
    if normalized.startswith("plugin/"):
        return normalized
    return None


def _classify_crosshair_verbose(body: str) -> ClassifiedLine | None:
    """Pick milestone lines from ``crosshair check -v`` / ``cover -v`` stderr."""
    match = VERBOSE_ANALYZE_FN.search(body)
    if match:
        return ClassifiedLine("CHECK PROGRESS", f"analyzing {match.group(1)}", body, show_stats=False)

    match = VERBOSE_ANALYZE_CLASS.search(body)
    if match:
        return ClassifiedLine("CHECK PROGRESS", f"class {match.group(1)}", body, show_stats=False)

    match = VERBOSE_ANALYZE_COND.search(body)
    if match:
        kind = match.group(1).lower()
        expr = match.group(2).strip().strip('"')[:80]
        return ClassifiedLine("CHECK PROGRESS", f"{kind}: {expr}", body, show_stats=False)

    if CROSSHAIR_INTERNAL.search(body):
        # One stable detail string so the summary does not repeat stack noise.
        return ClassifiedLine("CHECK ERROR", "CrossHairInternal engine crash", body)

    return None


def classify_line(line: str, mode: str) -> ClassifiedLine | None:
    """Classify one CrossHair or exploration line. Returns None to suppress noise."""
    stripped = line.strip()
    if not stripped:
        return None

    # Check: Tracebacks are hard failures (engine / uncaught).
    # Cover: app code often log.exception() during path exploration (e.g. payload_codec
    # unpacking garbage envelopes). Those print Traceback headers mid-run and are
    # explore noise — not CrossHair process death. Fail cover on CrossHairInternal or
    # non-zero process exit instead (see run_crosshair).
    if stripped.startswith("Traceback (most recent call last)"):
        if mode == "cover":
            return ClassifiedLine("COVER EXPLORE", "Traceback (path exploration)", stripped)
        if mode == "auto":
            # Pipe auto: treat like check (safer); cover-all always passes mode=cover.
            return ClassifiedLine("CHECK ERROR", "Traceback (CrossHair engine)", stripped)
        return ClassifiedLine("CHECK ERROR", "Traceback (CrossHair engine)", stripped)

    if CROSSHAIR_INTERNAL.search(stripped) and not VERBOSE_PREFIX.match(stripped):
        tag = "COVER FATAL" if mode == "cover" else "CHECK ERROR"
        return ClassifiedLine(tag, "CrossHairInternal engine crash", stripped)

    # CrossHair -v dumps File/TypeError stacks for CrosshairUnsupported path exploration
    # (format_stack in CrosshairUnsupported.__init__). Those are not process Tracebacks —
    # real crashes start with "Traceback (most recent call last)" above. Suppress in both modes.
    if TRACE_FILE.match(stripped) or (TRACE_LINE.match(stripped) and not stripped.startswith("Traceback")):
        return None

    if mode in ("check", "auto"):
        match = CHECK_LINE.match(stripped)
        if match:
            # Keep path/line for jumping to the contract; prefer repo-relative when under plugin/.
            raw_file = match.group("file")
            plugin_path = _plugin_relpath(raw_file)
            loc = f"{plugin_path or Path(raw_file).name}:{match.group('line')}"
            msg = match.group("msg")
            if match.group("level") == "error":
                return ClassifiedLine("CHECK ERROR", f"{loc}  {msg}", stripped)
            if "Confirmed over all paths" in msg:
                return ClassifiedLine("CHECK CONFIRMED", loc, stripped)
            if "Not confirmed" in msg:
                return ClassifiedLine("CHECK NOT_CONFIRMED", loc, stripped)
            if "Unable to meet precondition" in msg:
                short = msg.split(" at ", 1)[0]
                return ClassifiedLine("CHECK UNABLE", f"{loc}  {short}", stripped)

        if VERBOSE_PREFIX.match(stripped):
            body = _strip_crosshair_verbose(stripped)
            if body.startswith("at (") or "choose_possible()" in body or "gen_args()" in body:
                return None
            if "pre_path_hook()" in body or "find_key_in_heap()" in body:
                return None
            verbose = _classify_crosshair_verbose(body)
            if verbose is not None:
                return verbose
            return None

    if mode in ("cover", "auto"):
        if COVER_EXAMPLE.match(stripped) and not stripped.startswith("payload_codec"):
            return ClassifiedLine("COVER EXAMPLE", stripped[:120], stripped)
        if stripped.startswith("payload_codec"):
            return ClassifiedLine("COVER EXPLORE", stripped[:120], stripped)
        if "Uneven row lengths" in stripped:
            return ClassifiedLine("COVER EXPLORE", stripped[:120], stripped)

        if VERBOSE_PREFIX.match(stripped):
            body = _strip_crosshair_verbose(stripped)
            if CROSSHAIR_INTERNAL.search(body):
                return ClassifiedLine("COVER FATAL", "CrossHairInternal engine crash", stripped)
            if "path_cover" in body or "analyze_function()" in body:
                match = VERBOSE_ANALYZE_FN.search(body)
                if match:
                    return ClassifiedLine("COVER PROGRESS", f"cover {match.group(1)}", body, show_stats=False)
            return None

    return None


def update_stats(stats: StreamStats, classified: ClassifiedLine) -> None:
    tag = classified.tag
    if tag == "CHECK CONFIRMED":
        stats.confirmed += 1
    elif tag == "CHECK NOT_CONFIRMED":
        stats.not_confirmed += 1
    elif tag == "CHECK UNABLE":
        stats.unable += 1
    elif tag == "CHECK ERROR":
        stats.check_errors += 1
        stats.record_error(classified.detail)
    elif tag == "CHECK PROGRESS":
        stats.progress += 1
    elif tag == "COVER EXAMPLE":
        stats.examples += 1
    elif tag == "COVER EXPLORE":
        stats.explore += 1
    elif tag == "COVER FATAL":
        stats.cover_errors += 1
        stats.record_error(classified.detail)
    elif tag in ("COVER PROGRESS",):
        stats.progress += 1


def effective_mode(tag: str, default_mode: str) -> str:
    if tag.startswith("CHECK"):
        return "check"
    if tag.startswith("COVER"):
        return "cover"
    return default_mode


def format_event(classified: ClassifiedLine, stats: StreamStats, mode: str) -> str:
    width = 22
    head = f"[{classified.tag:<{width}}] {classified.detail}"
    if not classified.show_stats:
        return head
    emode = effective_mode(classified.tag, mode)
    return f"{head}\n  -> {stats.summary(emode)}"


def stream_lines(
    lines: Iterator[str],
    *,
    mode: str,
    out: TextIO,
    raw: bool,
    quiet: bool,
) -> StreamStats:
    stats = StreamStats()
    seen_progress: set[str] = set()
    for line in lines:
        stats.lines += 1
        classified = classify_line(line, mode)
        if classified is None:
            stats.suppressed += 1
            if raw:
                out.write(f"[RAW] {line.rstrip()}\n")
                out.flush()
            continue
        if classified.tag.endswith("PROGRESS"):
            if classified.detail in seen_progress:
                stats.suppressed += 1
                continue
            seen_progress.add(classified.detail)
        update_stats(stats, classified)
        if quiet:
            if classified.tag.endswith("ERROR") or classified.tag == "COVER FATAL":
                out.write(format_event(classified, stats, mode) + "\n")
                out.flush()
            continue
        out.write(format_event(classified, stats, mode) + "\n")
        out.flush()
    return stats


def print_error_summary(stats: StreamStats, out: TextIO) -> None:
    """Reprint unique failures so they are not buried under progress spam."""
    details = stats.error_details or []
    if not details:
        return
    out.write("\n=== ERRORS TO FIX ===\n")
    for index, detail in enumerate(details, start=1):
        out.write(f"  {index}. {detail}\n")
    out.write(
        "  (contract `: error:` lines and CrossHairInternal crashes; "
        "NOT_CONFIRMED/UNABLE are not listed)\n"
    )
    out.flush()


def print_banner(
    stats: StreamStats,
    mode: str,
    exit_code: int,
    out: TextIO,
    *,
    label: str | None = None,
) -> None:
    mode_label = mode.upper()
    failed = stats.failure_count > 0 or exit_code not in (0,)
    status = "FAIL" if failed else "DONE"
    out.write(f"\n=== CrossHair {mode_label} {status} (exit {exit_code}) ===\n")
    if label:
        out.write(f"  {label}\n")
    out.write(
        f"  lines read: {stats.lines} (suppressed {stats.suppressed})\n"
        f"  {stats.summary(mode)}\n"
    )
    if mode in ("check", "auto"):
        out.write(
            "  check legend: PROGRESS=verbose milestone | CONFIRMED/NOT_CONFIRMED/UNABLE/ERROR=contract result\n"
        )
    if mode in ("cover", "auto"):
        out.write(
            "  cover legend: EXAMPLE=input that adds coverage | EXPLORE=path via log/exception\n"
        )
    print_error_summary(stats, out)
    out.flush()


def find_crosshair() -> str:
    path = shutil.which("crosshair")
    if path:
        return path
    venv = Path(".venv/bin/crosshair")
    if venv.exists():
        return str(venv)
    raise SystemExit("crosshair not found on PATH or in .venv/bin/")


def discover_deal_plugin_files(plugin_root: Path | None = None) -> list[Path]:
    """Return ``plugin/**/*.py`` files that contain ``@deal.`` (instrumented modules)."""
    root = plugin_root if plugin_root is not None else Path("plugin")
    if not root.is_dir():
        raise SystemExit(f"plugin root not found: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py" and path.stat().st_size == 0:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "@deal." in text:
            files.append(path)
    return files


class _TeeTextIO:
    """Write formatted CrossHair output to stdout and a log file."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, s: str) -> int:
        for stream in self._streams:
            stream.write(s)
        return len(s)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def run_crosshair(
    command: str,
    crosshair_args: list[str],
    mode: str,
    raw: bool,
    quiet: bool,
    *,
    out: TextIO | None = None,
    label: str | None = None,
) -> tuple[int, StreamStats]:
    """Spawn CrossHair and stream output. Returns ``(exit_code, stats)``."""
    crosshair_path = find_crosshair()
    dest = out if out is not None else sys.stdout
    proc = subprocess.Popen(
        [crosshair_path, command, *crosshair_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    stats = stream_lines(proc.stdout, mode=mode, out=dest, raw=raw, quiet=quiet)
    proc_code = proc.wait()
    # Counterexamples / engine fatals from the stream always fail the run.
    if stats.failure_count > 0:
        exit_code = 1
    elif proc_code not in (0, None):
        # CrossHair exited non-zero without a classified : error: (e.g. internal crash).
        detail = (
            f"CrossHair process exited {proc_code} without classified contract errors "
            f"(engine crash or unexpected failure; re-run with --raw)"
        )
        dest.write(f"[CHECK ERROR           ] {detail}\n")
        dest.flush()
        stats.check_errors += 1
        stats.record_error(detail)
        exit_code = 1 if proc_code == 1 else proc_code
    else:
        exit_code = 0
    print_banner(stats, mode, exit_code, dest, label=label)
    return exit_code, stats


def _pipe_mode(mode: str, raw: bool, quiet: bool) -> int:
    if sys.stdin.isatty():
        sys.stderr.write(
            "Reading CrossHair output from stdin. Example:\n"
            f"  crosshair {mode} -v --report_all TARGET 2>&1 | "
            f"python scripts/crosshair_stream.py {mode}\n"
        )
    stats = stream_lines(sys.stdin, mode=mode, out=sys.stdout, raw=raw, quiet=quiet)
    # Fail on analysis errors even when CrossHair itself exits 0 with --report_all.
    exit_code = 0 if stats.failure_count == 0 else 1
    print_banner(stats, mode, exit_code, sys.stdout)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Filter CrossHair check/cover output (pipe crosshair -v through this script)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  crosshair check -v --report_all plugin/scripting/payload_codec.py 2>&1 \\\n"
            "      | python scripts/crosshair_stream.py check\n"
            "  python scripts/crosshair_stream.py run check -- -v --report_all plugin/scripting/payload_codec.py\n"
            "  make crosshair-check\n"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("check", "cover", "run"),
        help="check|cover = read stdin; run = spawn crosshair",
    )
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="With run: crosshair args after --")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only errors/fatals and final banner")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Also print suppressed lines as [RAW] (crosshair -v spam)",
    )

    args = parser.parse_args(argv)

    if args.command in ("check", "cover"):
        return _pipe_mode(args.command, args.raw, args.quiet)

    if args.command == "run":
        rest = args.rest
        if not rest:
            parser.error("run requires crosshair subcommand: run check ... or run cover ...")
        ch_cmd = rest[0]
        if ch_cmd not in ("check", "cover"):
            parser.error("run first arg must be check or cover")
        ch_args = rest[1:]
        if ch_args and ch_args[0] == "--":
            ch_args = ch_args[1:]
        exit_code, _stats = run_crosshair(ch_cmd, ch_args, ch_cmd, args.raw, args.quiet)
        return exit_code

    # No command: stdin pipe, default check if piped
    if not sys.stdin.isatty():
        return _pipe_mode("check", args.raw, args.quiet)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
