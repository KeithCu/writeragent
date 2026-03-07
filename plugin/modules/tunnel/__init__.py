"""Tunnel module — manages tunnel providers for exposing HTTP externally.

The parent module owns the subprocess lifecycle; each child provider supplies
its binary, command-line, and URL-parsing logic via the TunnelProvider protocol.
"""

import logging
import re
import subprocess
import threading

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("writeragent.tunnel")

# Windows: hide subprocess console window
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class TunnelError(Exception):
    """General tunnel error."""


class TunnelAuthError(TunnelError):
    """Provider requires authentication credentials."""


def get_provider_options(services):
    """Return available tunnel providers as option dicts.

    Called dynamically by the options handler to populate the provider
    select widget. Discovers registered providers from the tunnel_manager.
    """
    try:
        if services and hasattr(services, "tunnel_manager"):
            mgr = services.tunnel_manager
            return [
                {"value": name, "label": name.title()}
                for name in sorted(mgr.providers)
            ]
    except Exception:
        log.debug("get_provider_options: services not ready yet")
    return []


class TunnelManager:
    """Manages tunnel subprocess lifecycle using pluggable providers."""

    def __init__(self, config_svc, events):
        self.providers = {}
        self._process = None
        self._public_url = None
        self._active_provider = None
        self._lock = threading.Lock()
        self._config_svc = config_svc
        self._events = events

    def register_provider(self, name, provider):
        self.providers[name] = provider
        log.info("Tunnel provider registered: %s", name)

    def get_provider(self, name):
        return self.providers.get(name)

    @property
    def public_url(self):
        return self._public_url

    @property
    def is_running(self):
        return self._process is not None and self._process.poll() is None

    # ── Binary check ──────────────────────────────────────────────────

    def _check_binary(self, provider):
        """Verify the provider binary is installed. Returns True if OK."""
        if not provider.version_args:
            return True
        try:
            result = subprocess.run(
                provider.version_args,
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATION_FLAGS,
            )
            log.info("%s version: %s", provider.name,
                     result.stdout.strip() or result.stderr.strip())
            return True
        except FileNotFoundError:
            log.error(
                "%s binary '%s' not found. Install from: %s",
                provider.name, provider.binary_name, provider.install_url,
            )
            return False
        except Exception:
            log.exception("Error checking %s binary", provider.name)
            return False

    # ── Subprocess lifecycle ──────────────────────────────────────────

    def _run_and_parse(self, cmd, url_regex, provider):
        """Run the tunnel command, parse stdout for the public URL.

        Called in a daemon thread. Sets self._public_url when found.
        """
        log.info("Running: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_CREATION_FLAGS,
            )
        except FileNotFoundError:
            log.error("Binary not found: %s", cmd[0])
            return
        except Exception:
            log.exception("Failed to start tunnel process")
            return

        self._process = proc
        pattern = re.compile(url_regex) if url_regex else None

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                log.debug("[%s] %s", provider.name, line)

                if self._public_url:
                    continue

                # Try provider custom parsing first
                try:
                    custom_url = provider.parse_line(line)
                except TunnelAuthError:
                    log.error("Authentication required for %s", provider.name)
                    self._stop_process()
                    return
                except Exception:
                    custom_url = None

                if custom_url:
                    self._public_url = custom_url
                    log.info("Tunnel URL (custom): %s", self._public_url)
                    self._emit_started(provider)
                    continue

                # Fallback: regex matching
                if pattern:
                    m = pattern.search(line)
                    if m:
                        self._public_url = m.group(1)
                        log.info("Tunnel URL (regex): %s", self._public_url)
                        self._emit_started(provider)

        except Exception:
            log.exception("Error reading tunnel output")
        finally:
            ret = proc.wait()
            log.info("Tunnel process exited with code %s", ret)
            self._process = None
            if self._public_url:
                self._public_url = None
                self._emit_stopped("process_exited")

    def _emit_started(self, provider):
        if self._events:
            self._events.emit(
                "tunnel:started",
                public_url=self._public_url,
                provider=provider.name,
            )

    def _emit_stopped(self, reason):
        if self._events:
            self._events.emit("tunnel:stopped", reason=reason)

    def _stop_process(self):
        """Terminate the running tunnel process."""
        proc = self._process
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception:
            log.exception("Error stopping tunnel process")
        finally:
            self._process = None

    # ── Public API ────────────────────────────────────────────────────

    def start_tunnel(self):
        """Start the configured tunnel provider in a background thread."""
        with self._lock:
            if self.is_running:
                log.info("Tunnel already running at %s", self._public_url)
                return

            cfg = self._config_svc.proxy_for("tunnel")
            provider_name = cfg.get("provider")
            if not provider_name:
                log.info("Tunnel enabled but no provider selected")
                return

            provider = self.providers.get(provider_name)
            if provider is None:
                log.warning("Tunnel provider not found: %s", provider_name)
                return

            if not self._check_binary(provider):
                return

            # Get HTTP port and scheme from config
            http_cfg = self._config_svc.proxy_for("http")
            port = http_cfg.get("port", 8766)
            scheme = "https" if http_cfg.get("use_ssl") else "http"

            # Provider-specific config (now merged into 'tunnel')
            provider_cfg = self._config_svc.proxy_for("tunnel")
            try:
                provider.pre_start(provider_cfg)
            except Exception:
                log.exception("Provider pre_start failed for %s", provider_name)
                return

            # Build command
            try:
                cmd, url_regex = provider.build_command(port, scheme,
                                                        provider_cfg)
            except Exception:
                log.exception("Failed to build tunnel command for %s",
                              provider_name)
                return

            self._active_provider = provider
            self._public_url = None

            # Check for pre-known URL (e.g. named cloudflare tunnel)
            pre_url = getattr(provider, "get_known_url", lambda c: None)(
                provider_cfg)
            if pre_url:
                self._public_url = pre_url
                log.info("Tunnel URL (known): %s", self._public_url)

            # Start in daemon thread
            t = threading.Thread(
                target=self._run_and_parse,
                args=(cmd, url_regex, provider),
                daemon=True,
                name="tunnel-%s" % provider_name,
            )
            t.start()

            if pre_url:
                self._emit_started(provider)

    def stop_tunnel(self):
        """Stop the current tunnel process."""
        with self._lock:
            provider = self._active_provider
            had_url = self._public_url is not None

            self._stop_process()
            self._public_url = None
            self._active_provider = None

            # Post-stop hook
            if provider:
                try:
                    provider_cfg = self._config_svc.proxy_for("tunnel")
                    provider.post_stop(provider_cfg)
                except Exception:
                    log.exception("Provider post_stop failed for %s",
                                  provider.name)

            if had_url:
                self._emit_stopped("stopped")


class TunnelModule(ModuleBase):

    def initialize(self, services):
        self._services = services
        self._manager = TunnelManager(services.config, services.events)
        services.register_instance("tunnel_manager", self._manager)

        # Register built-in providers (consolidated from tunnel_* modules)
        from .providers.bore import BoreProvider
        from .providers.cloudflare import CloudflareProvider
        from .providers.ngrok import NgrokProvider
        from .providers.tailscale import TailscaleProvider

        self._manager.register_provider("bore", BoreProvider())
        self._manager.register_provider("cloudflare", CloudflareProvider())
        self._manager.register_provider("ngrok", NgrokProvider())
        self._manager.register_provider("tailscale", TailscaleProvider())

        if hasattr(services, "events"):
            services.events.subscribe("config:changed",
                                      self._on_config_changed)

    def start_background(self, services):
        cfg = services.config.proxy_for(self.name)
        if cfg.get("auto_start"):
            self._manager.start_tunnel()

    def _on_config_changed(self, **data):
        key = data.get("key", "")
        if not key.startswith("tunnel."):
            return
        cfg = self._services.config.proxy_for(self.name)
        if cfg.get("auto_start"):
            # Restart tunnel to pick up new config
            self._manager.stop_tunnel()
            self._manager.start_tunnel()
        else:
            self._manager.stop_tunnel()

    def shutdown(self):
        self._manager.stop_tunnel()
