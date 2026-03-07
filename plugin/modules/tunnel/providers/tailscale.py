"""Tailscale Funnel tunnel provider — pre/post reset, HTTPS support."""

import logging
import subprocess

log = logging.getLogger("writeragent.tunnel.tailscale")

# Windows: hide subprocess console window
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_RESET_COMMANDS = [
    ["tailscale", "funnel", "reset"],
    ["tailscale", "serve", "reset"],
]


class TailscaleProvider:
    """Tailscale Funnel: expose a local port via Tailscale network.

    HTTPS mode uses https+insecure:// to tell tailscale the backend is
    self-signed HTTPS. HTTP mode just uses the port number directly.
    Pre-start and post-stop run funnel/serve reset to ensure clean state.
    """

    name = "tailscale"
    binary_name = "tailscale"
    version_args = ["tailscale", "version"]
    install_url = "https://tailscale.com/download"

    def build_command(self, port, scheme, config):
        # We assume tailscale is already logged in
        if scheme == "https":
            # Tailscale needs to know if the target is HTTPS
            target = "https+insecure://127.0.0.1:%s" % port
        else:
            target = str(port)

        cmd = ["tailscale", "funnel", target]
        # Funnel logs: "Available at https://node-name.tailnet-name.ts.net/"
        url_regex = r"Available at (https://[\w.\-]+/)"
        return cmd, url_regex

    def parse_line(self, line):
        return None

    def pre_start(self, config):
        self._run_reset_commands()

    def post_stop(self, config):
        self._run_reset_commands()

    def _run_reset_commands(self):
        for cmd in _RESET_COMMANDS:
            try:
                subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=5,
                    creationflags=_CREATION_FLAGS,
                )
                log.debug("Reset: %s", " ".join(cmd))
            except Exception:
                log.debug("Reset command failed: %s", " ".join(cmd))
