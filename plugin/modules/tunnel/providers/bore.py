"""Bore tunnel provider — bore.pub relay."""

import logging

log = logging.getLogger("writeragent.tunnel.bore")


class BoreProvider:
    """Bore tunnel: exposes a local port via a bore relay server."""

    name = "bore"
    binary_name = "bore"
    version_args = ["bore", "--version"]
    install_url = "https://github.com/ekzhang/bore/releases"

    def build_command(self, port, scheme, config):
        server = config.get("server", "bore.pub")
        cmd = ["bore", "local", str(port), "--to", server]
        # bore outputs "listening at <host>:<port>"
        url_regex = r"listening at ([\w.\-]+:\d+)"
        return cmd, url_regex

    def parse_line(self, line):
        return None

    def pre_start(self, config):
        pass

    def post_stop(self, config):
        pass
