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
