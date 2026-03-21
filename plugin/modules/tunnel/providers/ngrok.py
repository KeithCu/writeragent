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
"""Ngrok tunnel provider — JSON log parsing, authtoken support."""

import json
import logging

from plugin.modules.tunnel import TunnelAuthError

log = logging.getLogger("writeragent.tunnel.ngrok")


class NgrokProvider:
    """Ngrok tunnel: HTTP tunnels with JSON log output.

    Uses --log-format json so we can parse structured output instead of
    relying on regex. Detects ERR_NGROK_105 (missing authtoken).
    """

    name = "ngrok"
    binary_name = "ngrok"
    version_args = ["ngrok", "version"]
    install_url = "https://ngrok.com/download"

    def build_command(self, port, scheme, config):
        cmd = [
            "ngrok", "http",
            "%s://localhost:%s" % (scheme, port),
            "--log", "stdout",
            "--log-format", "json",
        ]
        authtoken = config.get("authtoken", "")
        if authtoken:
            cmd.extend(["--authtoken", authtoken])

        # No regex needed — we use custom JSON parsing in parse_line
        return cmd, None

    def parse_line(self, line):
        if not line.startswith("{"):
            return None
        try:
            data = json.loads(line)
            # Check for tunnel started message
            if data.get("msg") == "started tunnel" and "url" in data:
                return data["url"]

            # Error detection: ERR_NGROK_105 (authtoken missing/invalid)
            # This appears in 'err' or as a log message
            err = data.get("err") or data.get("error")
            if err and "ERR_NGROK_105" in str(err):
                raise TunnelAuthError("ngrok authtoken required")

        except Exception:
            pass
        return None

    def pre_start(self, config):
        pass

    def post_stop(self, config):
        pass
