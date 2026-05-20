#!/usr/bin/env python3
"""
Parse writeragent_debug.log and compute T1/T2 timing for tool-calling rounds.

T1 = time from "Tool loop round N: sending" to stream end
     (one round's API time: connection + model streaming).
     Stream end = "streaming_loop: [DONE] received", "stream_request_with_tools: stream ended", or first "[Chat] Tool call:" after that send (some endpoints end with finish_reason, so [DONE] may not be logged).
T2 = time from stream end to "Tool loop round N+1: sending"
     (our work: drain, execute tools, start next worker).

Usage:
  python scripts/analyze_tool_call_timing.py [path/to/writeragent_debug.log]
  If no path given, tries writeragent_debug.log under the LibreOffice user config dir.
"""

import re
import sys
from datetime import datetime
from pathlib import Path


# Debug log line format: "YYYY-MM-DD HH:MM:SS.mmm | [Context] msg"
LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| (.+)$")


def parse_timestamp(s):
    """Parse 'YYYY-MM-DD HH:MM:SS.mmm' to seconds since epoch for delta math."""
    try:
        if len(s) >= 23 and s[19] == ".":
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            ms = int(s[20:23].ljust(3, "0")[:3])
            return dt.timestamp() + ms / 1000.0
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except (ValueError, IndexError):
        return None


def find_log_path():
    """Default log locations (extension writes to user config, sometimes under config/)."""
    candidates = [
        Path.home() / ".config" / "libreoffice" / "4" / "user" / "config" / "writeragent_debug.log",
        Path.home() / ".config" / "libreoffice" / "4" / "user" / "writeragent_debug.log",
        Path.home() / ".config" / "libreoffice" / "24" / "user" / "config" / "writeragent_debug.log",
        Path.home() / ".config" / "libreoffice" / "24" / "user" / "writeragent_debug.log",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # return first as default for "not found" message


def analyze(log_path):
    path = Path(log_path)
    if not path.exists():
        print("Log file not found: %s" % path, file=sys.stderr)
        print("Reproduce the delay (Calc Chat, 2+ tool rounds), then run this script.", file=sys.stderr)
        return 1

    events = []  # (timestamp, 'send', round_index) or (timestamp, 'done', None)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_LINE_RE.match(line.strip())
            if not m:
                continue
            ts_str, rest = m.group(1), m.group(2)
            t = parse_timestamp(ts_str)
            if t is None:
                continue
            if "Tool loop round " in rest and " sending " in rest and " to API" in rest:
                r = re.search(r"Tool loop round (\d+)", rest)
                if r:
                    events.append((t, "send", int(r.group(1))))
            if "streaming_loop: [DONE] received" in rest or "stream_request_with_tools: stream ended" in rest:
                events.append((t, "done", None))
            if "[Chat] Tool call:" in rest:
                events.append((t, "done", None))

    events.sort(key=lambda x: (x[0], x[1] == "send"))  # send before done if same second

    sends = [(t, r) for t, typ, r in events if typ == "send"]
    if not sends:
        print("No 'Tool loop round N: sending ... to API' lines found in %s" % path, file=sys.stderr)
        print("Reproduce with Calc Chat (tool-calling), then run again.", file=sys.stderr)
        return 1

    print("Tool-calling timing analysis: %s" % path)
    print()

    # Pair each "send" with the next "done" that appears after it chronologically.
    done_times = [t for t, typ, _ in events if typ == "done"]
    j = 0
    for i, (send_t, r) in enumerate(sends):
        while j < len(done_times) and done_times[j] < send_t:
            j += 1
        if j >= len(done_times):
            print("Round %d: send at %.3f -> no stream end found after it" % (r, send_t))
            break
        done_t = done_times[j]
        t1 = done_t - send_t
        print("Round %d: send at %.3f -> stream end at %.3f  =>  T1 = %.2f s (API/stream)" % (r, send_t, done_t, t1))
        j += 1
        if i + 1 < len(sends):
            next_send_t = sends[i + 1][0]
            t2 = next_send_t - done_t
            note = ""
            if t2 < 0 or t2 > 60:
                note = "  (likely different session - ignore)"
            print("        stream end -> next round send at %.3f  =>  T2 = %.2f s (our code)%s" % (next_send_t, t2, note))
        print()

    print("Interpretation: If T1 is large (e.g. 5-25s), delay is model/network. T2 should be <1s; if negative or huge, events are from different sessions.")
    return 0


def main():
    if len(sys.argv) >= 2:
        log_path = sys.argv[1]
    else:
        log_path = find_log_path()
    return analyze(log_path)


if __name__ == "__main__":
    sys.exit(main())
