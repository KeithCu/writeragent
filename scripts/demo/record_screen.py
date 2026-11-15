#!/usr/bin/python3
# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later
"""Record the whole screen to a .mkv on Wayland, for the product video.

    /usr/bin/python3 scripts/demo/record_screen.py build/demo/video/scene1.mkv

Stop it with Ctrl+C or SIGTERM; the file is finished cleanly either way.

Why this exists: on KDE Wayland only the compositor can hand out screen
contents, through the xdg-desktop-portal ScreenCast interface, and
``spectacle -R`` could not be started reliably from a script. This asks the
portal for one monitor and pipes the PipeWire stream into GStreamer (x264 in
Matroska, which stays playable even if the process is killed).

The first run shows the desktop's "share screen" dialog once. The portal's
restore token is kept next to the output file, so later runs start unprompted.
Run it with the system Python: it needs ``dbus`` and ``gi`` (GStreamer), which
the project venv does not have.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Any

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
TOKEN_FILE = ".portal_restore_token"


def build_pipeline(fd: int, node_id: int, out: str, fps: int = 30) -> str:
    """GStreamer launch line: PipeWire node -> constant frame rate -> x264 -> Matroska.

    ``videorate`` matters: the compositor only sends a frame when the screen
    changes, and a still screen would otherwise shorten the recording.
    """
    return (
        f"pipewiresrc fd={fd} path={node_id} do-timestamp=true keepalive-time=1000 resend-last=true"
        f" ! videoconvert ! videorate ! video/x-raw,framerate={fps}/1"
        f" ! x264enc speed-preset=superfast pass=qual quantizer=21 key-int-max={fps * 2}"
        f' ! matroskamux ! filesink location="{out}"'
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = Path(argv[1]).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    token_path = out.parent / TOKEN_FILE

    import dbus
    import gi
    from dbus.mainloop.glib import DBusGMainLoop

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    DBusGMainLoop(set_as_default=True)
    Gst.init(None)
    bus = dbus.SessionBus()
    portal = bus.get_object(PORTAL, PORTAL_PATH)
    cast = dbus.Interface(portal, SCREENCAST)
    loop = GLib.MainLoop()
    sender = bus.get_unique_name()[1:].replace(".", "_")
    state: dict[str, Any] = {"n": 0, "pipeline": None, "rc": 1}

    def call(method: Any, options: dict[str, Any], on_done: Any, *args: Any) -> None:
        """Portal calls answer through a Request object's Response signal, not the return value."""
        state["n"] += 1
        token = "wa%d_%d" % (os.getpid(), state["n"])
        path = "/org/freedesktop/portal/desktop/request/%s/%s" % (sender, token)
        bus.add_signal_receiver(on_done, "Response", REQUEST, PORTAL, path)
        options["handle_token"] = token
        method(*args, options, dbus_interface=SCREENCAST)

    def fail(msg: str) -> None:
        print("record_screen: %s" % msg, file=sys.stderr, flush=True)
        loop.quit()

    def on_session(code: int, results: dict[str, Any]) -> None:
        if code != 0:
            return fail("CreateSession refused (%d)" % code)
        state["session"] = results["session_handle"]
        # types=1 is a monitor, cursor_mode=2 draws the pointer into the video,
        # persist_mode=2 lets the saved token skip the dialog next time.
        options: dict[str, Any] = {"types": dbus.UInt32(1), "cursor_mode": dbus.UInt32(2), "persist_mode": dbus.UInt32(2)}
        if token_path.exists():
            options["restore_token"] = token_path.read_text().strip()
        call(cast.SelectSources, options, on_sources, state["session"])

    def on_sources(code: int, results: dict[str, Any]) -> None:
        if code != 0:
            return fail("SelectSources refused (%d)" % code)
        call(cast.Start, {}, on_start, state["session"], "")

    def on_start(code: int, results: dict[str, Any]) -> None:
        if code != 0:
            return fail("screen sharing was not approved (%d)" % code)
        if results.get("restore_token"):
            token_path.write_text(str(results["restore_token"]))
        node_id = int(results["streams"][0][0])
        fd = cast.OpenPipeWireRemote(state["session"], {}).take()
        pipeline = Gst.parse_launch(build_pipeline(fd, node_id, str(out)))
        state["pipeline"] = pipeline
        gst_bus = pipeline.get_bus()
        gst_bus.add_signal_watch()
        gst_bus.connect("message::eos", lambda *unused: (state.update(rc=0), loop.quit()))
        gst_bus.connect("message::error", lambda unused, msg: fail(str(msg.parse_error()[0])))
        pipeline.set_state(Gst.State.PLAYING)
        print("RECORDING %s" % out, flush=True)

    def stop(*unused: Any) -> bool:
        pipeline = state["pipeline"]
        if pipeline is None:
            loop.quit()
        else:
            pipeline.send_event(Gst.Event.new_eos())  # flush the encoder; the EOS message ends the loop
        return False

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, stop)
    call(cast.CreateSession, {"session_handle_token": "wa%d" % os.getpid()}, on_session)
    loop.run()
    if state["pipeline"] is not None:
        state["pipeline"].set_state(Gst.State.NULL)
    return int(state["rc"])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
