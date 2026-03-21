# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Cloudflare tunnel provider — quick or named tunnels via cloudflared."""

import logging

log = logging.getLogger("writeragent.tunnel.cloudflare")


class CloudflareProvider:
    """Cloudflare Tunnel: quick (random URL) or named (stable domain).

    Quick mode: cloudflared creates a temporary trycloudflare.com URL.
    Named mode: uses a pre-configured tunnel name with a known public URL.
    """

    name = "cloudflare"
    binary_name = "cloudflared"
    version_args = ["cloudflared", "--version"]
    install_url = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"

    def build_command(self, port, scheme, config):
        tunnel_name = config.get("tunnel_name", "")

        if tunnel_name:
            # Named tunnel — stable domain, pre-configured via cloudflared
            cmd = [
                "cloudflared", "tunnel",
                "--no-autoupdate",
                "run", tunnel_name,
            ]
            # Named tunnels log the URL differently; may need custom regex
            url_regex = r"(https://[\w.-]+)"
        else:
            # Quick tunnel — temporary URL
            cmd = [
                "cloudflared", "tunnel",
                "--no-autoupdate",
                "--url", "http://localhost:%s" % port,
            ]
            url_regex = r"(https://[\w.-]+\.trycloudflare\.com)"

        return cmd, url_regex

    def parse_line(self, line):
        return None

    def pre_start(self, config):
        pass

    def post_stop(self, config):
        pass

    def get_known_url(self, config):
        """If tunnel_name is set, return the expected public URL if known."""
        return config.get("public_url")
