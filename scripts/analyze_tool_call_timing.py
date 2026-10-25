#!/usr/bin/env python3
"""
Parse writeragent_debug.log and compute T1/T2 timing for tool-calling rounds.

T1 = time from "Tool loop round N: sending" to stream end
     (one round's API time: connection + model streaming).
     Stream end = "streaming_loop: [DONE] received" or "LLM response stream finished:".
T2 = time from stream end to "Tool loop round N+1: sending"
     (our work: drain, execute tools, start next worker).

Note:
  log_level must be DEBUG in writeragent.json because the send line is log.debug.

Usage:
  python scripts/analyze_tool_call_timing.py [path/to/writeragent_debug.log]
  If no path given, tries writeragent_debug.log under the LibreOffice user config dir.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from eval_2_debug_log import debug_log_candidates, find_debug_log  # type: ignore

# Markers emitted by host code
SEND_MARKER = "Tool loop round "  # plugin/chatbot/tool_loop.py (log.debug)
DONE_MARKERS = (
    "streaming_loop: [DONE] received",  # plugin/framework/client/llm_client.py (log.info)
    "LLM response stream finished:",  # plugin/framework/client/llm_client.py (log.info)
)
PREFILTER_MARKERS = (SEND_MARKER,) + DONE_MARKERS

# Debug log line format: "YYYY-MM-DD HH:MM:SS[,.]mmm | [Context] msg"
LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d{3}) \| (.+)$")


def parse_timestamp(s: str) -> datetime | None:
    """Parse 'YYYY-MM-DD HH:MM:SS.mmm' to datetime."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def find_log_path():
    """Default log locations (shared with eval_2_debug_log / writeragent.json)."""
    found = find_debug_log()
    if found is not None:
        return found
    candidates = debug_log_candidates()
    return candidates[0]  # first candidate for the "not found" message


def analyze(log_path: str | Path) -> int:
    path = Path(log_path)
    if not path.exists():
        print("Log file not found: %s" % path, file=sys.stderr)
        print("Reproduce the delay (Calc Chat, 2+ tool rounds), then run this script.", file=sys.stderr)
        return 1

    events: set[tuple[datetime, str, int | None]] = set()
    lines = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            lines += 1
            if not any(marker in line for marker in PREFILTER_MARKERS):
                continue
            m = LOG_LINE_RE.match(line.rstrip("\r\n"))
            if not m:
                continue
            ts_str = f"{m.group(1)}.{m.group(2)}"
            dt = parse_timestamp(ts_str)
            if dt is None:
                continue
            rest = m.group(3)
            if SEND_MARKER in rest and " sending " in rest and " to API" in rest:
                r_match = re.search(r"Tool loop round (\d+)", rest)
                if r_match:
                    events.add((dt, "send", int(r_match.group(1))))
            if any(marker in rest for marker in DONE_MARKERS):
                events.add((dt, "done", None))

    ordered = sorted(events, key=lambda e: (e[0], e[1] != "send"))

    sends = [(dt, r) for dt, kind, r in ordered if kind == "send" and r is not None]
    dones = [dt for dt, kind, _ in ordered if kind == "done"]

    if not sends:
        size_mb = path.stat().st_size / (1024 * 1024)
        print(
            f"No 'Tool loop round N: sending ... to API' lines found in {path} ({size_mb:.1f} MB, {lines} lines)",
            file=sys.stderr,
        )
        print("Set log_level to DEBUG in writeragent.json; the send line is log.debug.", file=sys.stderr)
        return 1

    print(f"Tool-calling timing analysis: {path}")
    print()

    prev_round: int | None = None
    for i, (send_dt, r) in enumerate(sends):
        if prev_round is not None and r <= prev_round:
            print(f"(new session: round {r} after round {prev_round})")
        prev_round = r
        next_send = sends[i + 1][0] if i + 1 < len(sends) else None
        done_dt = next((d for d in dones if d > send_dt and (next_send is None or d < next_send)), None)
        send_str = send_dt.strftime("%H:%M:%S.%f")[:-3]
        if done_dt is None:
            print(f"Round {r}: send at {send_str} -> no stream end before the next send (incomplete)")
            print()
            continue
        done_str = done_dt.strftime("%H:%M:%S.%f")[:-3]
        t1 = (done_dt - send_dt).total_seconds()
        print(f"Round {r}: send at {send_str} -> stream end at {done_str}  =>  T1 = {t1:.2f} s (API/stream)")
        if i + 1 < len(sends):
            next_send_dt, next_r = sends[i + 1]
            if next_r > r:
                t2 = (next_send_dt - done_dt).total_seconds()
                next_send_str = next_send_dt.strftime("%H:%M:%S.%f")[:-3]
                print(f"        stream end -> next round send at {next_send_str}  =>  T2 = {t2:.2f} s (our code)")
        print()

    print(
        "Interpretation: If T1 is large (e.g. 5-25s), delay is model/network. T2 should be <1s; if negative or huge, events are from an unpaired or restarted round."
    )
    return 0


def main():
    if len(sys.argv) >= 2:
        log_path = sys.argv[1]
    else:
        log_path = find_log_path()
    return analyze(log_path)


if __name__ == "__main__":
    sys.exit(main())
