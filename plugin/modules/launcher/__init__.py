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
# Adapted from Nelson MCP
"""Launcher module — launches an external AI CLI tool connected to WriterAgent MCP.

The parent module owns the subprocess lifecycle and terminal detection;
each child provider supplies its binary, auto-config, and install scripts
via the CliProvider protocol.
"""

import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("writeragent.launcher")

# Windows: open subprocess in a new console window (not hidden)
_CREATION_FLAGS = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if sys.platform == "win32" else 0

def check_cli_installed(services):
    """Check if the selected CLI binary is available in PATH."""
    if not services or not hasattr(services, "launcher_manager"):
        return {"status": "unknown", "message": "Services not ready"}

    cfg = services.config.proxy_for("launcher")
    provider_name = cfg.get("provider")
    if not provider_name:
        return {"status": "unknown", "message": "No provider selected"}

    provider = services.launcher_manager.get_provider(provider_name)
    if provider is None:
        return {"status": "ko", "message": "Provider '%s' not found" % provider_name}

    if shutil.which(provider.binary_name):
        return {"status": "ok", "message": "'%s' found in PATH" % provider.binary_name}
    else:
        return {"status": "ko",
                "message": "'%s' not found — click Install" % provider.binary_name}


def run_install_for_provider(provider_name):
    """Run the install script for the given provider in a terminal.

    Called by each sub-module's on_install callback.
    """
    from plugin.framework.dialogs import msgbox
    from plugin.framework.uno_context import get_ctx
    from plugin.main import get_services

    log.debug("run_install_for_provider called: %s", provider_name)
    ctx = get_ctx()
    services = get_services()
    if not services:
        log.warning("run_install_for_provider: services not available")
        return

    mgr = services.launcher_manager
    provider = mgr.get_provider(provider_name)
    if provider is None:
        msgbox(ctx, "WriterAgent",
               "CLI provider '%s' not found." % provider_name)
        return

    # Pick platform script
    if sys.platform == "win32":
        script_name = "install.ps1"
    else:
        script_name = "install.sh"

    # Locate script in launcher's directory
    mod_dir = os.path.dirname(__file__)
    script_path = os.path.join(mod_dir, f"{provider_name}_scripts", script_name)

    if not os.path.isfile(script_path):
        msgbox(ctx, "WriterAgent",
               "Install script not found:\n%s" % script_path)
        return

    # Build command: run the script then wait for user input
    if sys.platform == "win32":
        # On Windows, CREATE_NEW_CONSOLE opens a new window — no terminal wrapper needed
        full_cmd = [
            "powershell", "-ExecutionPolicy", "Bypass", "-Command",
            "& '%s'; Write-Host; Write-Host 'Installation complete. Press Enter to close.';"
            " Read-Host" % script_path.replace("'", "''"),
        ]
    else:
        cfg = services.config.proxy_for("launcher")
        terminal = cfg.get("terminal") or ""
        try:
            term = _find_terminal(terminal)
        except Exception:
            log.exception("Terminal detection failed")
            msgbox(ctx, "WriterAgent", "Could not find a terminal emulator.")
            return

        cli_cmd = [
            "bash", "-c",
            "bash %s; echo; echo 'Installation complete. Press Enter to close.'; read"
            % shlex.quote(script_path),
        ]
        full_cmd = _build_terminal_cmd(term, cli_cmd)

    try:
        log.info("Running install script: %s", " ".join(str(c) for c in full_cmd))
        subprocess.Popen(
            full_cmd,
            start_new_session=True,
            creationflags=_CREATION_FLAGS,
        )
    except Exception:
        log.exception("Failed to launch install script")
        msgbox(ctx, "WriterAgent",
               "Failed to launch install script.")


def get_provider_options(services):
    """Return available CLI providers as option dicts.

    Called dynamically by the options handler to populate the provider
    select widget. Discovers registered providers from the launcher_manager.
    """
    try:
        log.debug("get_provider_options called with services: %s", "available" if services else "None")
        if services and hasattr(services, "launcher_manager"):
            mgr = services.launcher_manager
            options = [
                {"value": name, "label": prov.label if hasattr(prov, "label") else name.title()}
                for name, prov in sorted(mgr.providers.items())
            ]
            log.info("get_provider_options returning %d options: %s", len(options), [o["value"] for o in options])
            return options
        else:
            log.warning("get_provider_options: launcher_manager service not found in services")
    except Exception as e:
        log.error("get_provider_options exception: %s", e)
    return []


def get_active_provider_default_cwd(services):
    """Return default CWD of the currently selected provider."""
    try:
        if services and hasattr(services, "launcher_manager"):
            cfg = services.config.proxy_for("launcher")
            name = cfg.get("provider")
            if name:
                prov = services.launcher_manager.get_provider(name)
                if prov:
                    return prov.get_default_cwd()
    except Exception:
        pass
    return ""


def get_global_instructions_default(services):
    """Return default content for AI CLI instructions."""
    from plugin.framework.constants import (
        FORMATTING_RULES, CORE_DIRECTIVES, TRANSLATION_RULES,
        CALC_WORKFLOW, CALC_FORMULA_SYNTAX
    )

    # Start with a strong persona and technical guidance
    # We use f-string to inject the shared rules from constants.py
    base = f"""# WriterAgent MCP — AI CLI Instructions

You are an AI assistant helping the user work with a LibreOffice document through **WriterAgent MCP**. Your goal is to help the user create polished, professional documents.

## Core Directives

{CORE_DIRECTIVES}

{TRANSLATION_RULES}

## Formatting Rules

{FORMATTING_RULES}

## Calc & Spreadsheet Rules

{CALC_WORKFLOW}

{CALC_FORMULA_SYNTAX}

## Tool Usage Patterns

- **Writer**: Use `get_document_tree` to see the structure. Use `_mcp_` bookmarks for stable addressing.
- **Calc**: Use `get_sheet_summary` and `read_cell_range`. Use Excel-style A1 references.
- **Navigation**: Prefer stable locators (bookmarks, heading text) over paragraph indices.
- **Batching**: Prefer tools that operate on ranges or bulk data instead of many single-cell operations.

## Context Awareness

This file summarizes the current state of the project. AI Assistants: You SHOULD update this file if you learn something important about the user's workflow or project structure that would help future sessions.
"""
    return base


def get_unified_prompt(services):
    """Return the final prompt to be written to CLAUDE.md / AGENTS.md / etc."""
    cfg = services.config.proxy_for("launcher")
    instructions = cfg.get("global_ai_instructions")
    if not instructions or not str(instructions).strip():
        return get_global_instructions_default(services)
    return instructions


def write_unified_prompt(cwd, filename):
    """Write the unified prompt to the given file in the working directory."""
    from plugin.main import get_services
    services = get_services()
    if not services:
        log.warning("Could not write %s: services not available", filename)
        return False

    prompt = get_unified_prompt(services)
    prompt_path = os.path.join(cwd, filename)
    try:
        with open(prompt_path, "w") as f:
            f.write(prompt)
        log.info("Wrote unified prompt to %s", prompt_path)
        return True
    except Exception:
        log.exception("Failed to write %s", prompt_path)
        return False



def on_install_active_provider():
    """Action handler for the unified Install button."""
    import webbrowser
    from plugin.main import get_services
    from plugin.framework.dialogs import msgbox
    from plugin.framework.uno_context import get_ctx

    services = get_services()
    if not services:
        return

    cfg = services.config.proxy_for("launcher")
    name = cfg.get("provider")

    if not name:
        msgbox(get_ctx(), "WriterAgent", "Please select a provider first.")
        return

    provider = services.launcher_manager.get_provider(name)
    if provider is None:
        msgbox(get_ctx(), "WriterAgent", "CLI provider '%s' not found." % name)
        return

    install_url = getattr(provider, "install_url", None)
    if install_url:
        try:
            webbrowser.open(install_url)
        except Exception:
            log.exception("Failed to open install URL: %s", install_url)
            msgbox(get_ctx(), "WriterAgent", "Failed to open install URL.")
    else:
        msgbox(get_ctx(), "WriterAgent", "No install URL available for provider '%s'." % name)


def _find_terminal(configured):
    """Return terminal command (str or list).

    If *configured* is set, use it directly. Otherwise auto-detect.
    """
    if configured:
        return configured

    # Check $TERMINAL env var
    env_term = os.environ.get("TERMINAL")
    if env_term and shutil.which(env_term):
        return env_term

    if sys.platform == "win32":
        # Windows Terminal, then fallback to conhost
        if shutil.which("wt"):
            return "wt"
        return "conhost"

    if sys.platform == "darwin":
        return "open"  # handled specially in _build_terminal_cmd

    for term in [
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "mate-terminal",
        "tilix",
        "alacritty",
        "kitty",
        "xterm",
    ]:
        if shutil.which(term):
            return term

    return "xterm"


def _build_terminal_cmd(terminal, cli_cmd):
    """Build full command list: terminal + CLI command."""
    if sys.platform == "darwin" and terminal == "open":
        # macOS: open -a Terminal <script>
        shell_cmd = " ".join(shlex.quote(c) for c in cli_cmd)
        return ["osascript", "-e",
                'tell app "Terminal" to do script "%s"' % shell_cmd.replace('"', '\\"')]

    base = os.path.basename(terminal)

    # Windows terminals
    if base == "wt" or base == "wt.exe":
        return [terminal, "new-tab", "--", *cli_cmd]

    if base in ("conhost", "conhost.exe"):
        return [terminal, *cli_cmd]

    if base in ("gnome-terminal", "mate-terminal"):
        return [terminal, "--", *cli_cmd]

    if base == "tilix":
        return [terminal, "-e", " ".join(shlex.quote(c) for c in cli_cmd)]

    if base == "konsole":
        return [terminal, "-e", *cli_cmd]

    if base == "xfce4-terminal":
        return [terminal, "-e", " ".join(shlex.quote(c) for c in cli_cmd)]

    if base in ("alacritty", "kitty"):
        return [terminal, "-e", *cli_cmd]

    # Generic fallback (xterm and others): -e command args
    return [terminal, "-e", *cli_cmd]


class LauncherManager:
    """Registry for CLI providers."""

    def __init__(self):
        self.providers = {}

    def register_provider(self, name, provider):
        self.providers[name] = provider
        log.info("CLI provider registered: %s (total: %d)", name, len(self.providers))

    def get_provider(self, name):
        return self.providers.get(name)


class LauncherModule(ModuleBase):

    def initialize(self, services):
        self._services = services
        self._process = None
        self._lock = threading.Lock()
        self._manager = LauncherManager()
        
        # Register providers
        from .claude import ClaudeProvider
        from .gemini import GeminiProvider
        from .hermes import HermesProvider
        from .opencode import OpenCodeProvider
        for cls in (ClaudeProvider, GeminiProvider, HermesProvider, OpenCodeProvider):
            p = cls(services)
            self._manager.register_provider(p.name, p)
            
        services.register_instance("launcher_manager", self._manager)

    def shutdown(self):
        self._stop_process()

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action):
        if action == "launch_cli":
            self._action_launch()
        else:
            super().on_action(action)

    def get_menu_text(self, action):
        if action == "launch_cli":
            provider = self._get_provider_silent()
            label = provider.label if provider else "AI CLI"
            if self._is_running():
                return "%s Running" % label
            return "Launch %s" % label
        return None

    def get_menu_icon(self, action):
        if action == "launch_cli":
            return "running" if self._is_running() else "cli"
        return None

    # ── Process state ────────────────────────────────────────────────

    def _is_running(self):
        return self._process is not None and self._process.poll() is None

    def _stop_process(self):
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
            log.exception("Error stopping CLI process")
        finally:
            self._process = None

    # ── Helpers ──────────────────────────────────────────────────────

    def _get_provider_silent(self):
        """Return the selected provider or None (no UI)."""
        cfg = self._services.config.proxy_for(self.name)
        provider_name = cfg.get("provider")
        if not provider_name:
            return None
        return self._manager.get_provider(provider_name)

    def _get_provider(self):
        """Return the selected provider or None + show error."""
        from plugin.framework.dialogs import msgbox
        from plugin.framework.uno_context import get_ctx

        provider = self._get_provider_silent()
        if provider is not None:
            return provider

        cfg = self._services.config.proxy_for(self.name)
        provider_name = cfg.get("provider")
        if not provider_name:
            msgbox(get_ctx(), "WriterAgent",
                   "No AI CLI provider selected.\n"
                   "Go to Options → WriterAgent → Launcher to pick one.")
        else:
            msgbox(get_ctx(), "WriterAgent",
                   "CLI provider '%s' not found." % provider_name)
        return None

    def _get_mcp_url(self):
        """Build the MCP base URL from HTTP config."""
        http_cfg = self._services.config.proxy_for("http")
        port = http_cfg.get("port") or http_cfg.get("mcp_port") or 8765
        host = http_cfg.get("host") or "localhost"
        scheme = "https" if http_cfg.get("use_ssl") else "http"
        return "%s://%s:%s" % (scheme, host, port), host, port

    # ── Launch logic ─────────────────────────────────────────────────

    def _action_launch(self):
        from plugin.framework.dialogs import msgbox
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()

        if self._is_running():
            msgbox(ctx, "WriterAgent", "AI CLI is already running.")
            return

        provider = self._get_provider()
        if provider is None:
            return

        cfg = self._services.config.proxy_for(self.name)
        args_str = cfg.get("args") or ""
        auto_config = cfg.get("auto_config", True)
        terminal = cfg.get("terminal") or ""
        mcp_url, host, port = self._get_mcp_url()

        # Resolve working directory
        cwd = cfg.get("cwd") or provider.default_cwd
        os.makedirs(cwd, exist_ok=True)

        provider_cfg = self._services.config.proxy_for(
            "launcher.%s" % provider.name)

        # Check command exists
        if not shutil.which(provider.binary_name):
            msgbox(ctx, "WriterAgent",
                   "Command '%s' not found.\n"
                   "Make sure it is installed and in your PATH.\n\n"
                   "Use 'Install AI CLI' from the menu to get install instructions."
                   % provider.binary_name)
            return

        # Build CLI command
        cli_cmd = [provider.binary_name]

        # Provider-specific args
        cli_cmd.extend(provider.get_args(mcp_url, provider_cfg))

        # User override args with placeholder substitution
        if args_str:
            expanded = args_str.format(
                mcp_url=mcp_url,
                port=port,
                host=host,
            )
            cli_cmd.extend(shlex.split(expanded))

        # Build environment
        env = os.environ.copy()

        # Auto-config: write MCP config into cwd
        if auto_config:
            try:
                provider.setup_env(cwd, mcp_url)
            except Exception:
                log.exception("Auto-config failed for %s", provider.name)

        # Wrap CLI command so the terminal stays open on exit/crash
        if sys.platform == "win32":
            # On Windows, CREATE_NEW_CONSOLE opens a new window — no terminal wrapper
            # Quote args containing spaces for PowerShell
            ps_parts = []
            for c in cli_cmd:
                if " " in c or "'" in c:
                    ps_parts.append("'%s'" % c.replace("'", "''"))
                else:
                    ps_parts.append(c)
            inner = "& " + " ".join(ps_parts)
            full_cmd = [
                "powershell", "-Command",
                "%s; Write-Host; Write-Host 'CLI exited. Press Enter to close.';"
                " Read-Host" % inner,
            ]
        else:
            # Detect terminal (Linux/macOS only)
            try:
                term = _find_terminal(terminal)
            except Exception:
                log.exception("Terminal detection failed")
                msgbox(ctx, "WriterAgent", "Could not find a terminal emulator.")
                return

            if not shutil.which(term) and not (sys.platform == "darwin" and term == "open"):
                msgbox(ctx, "WriterAgent",
                       "Terminal '%s' not found.\n"
                       "Install it or set a different one in Options." % term)
                return

            shell_str = " ".join(shlex.quote(c) for c in cli_cmd)
            cli_cmd = [
                "bash", "-c",
                "%s; echo; echo 'CLI exited. Press Enter to close.'; read" % shell_str,
            ]
            full_cmd = _build_terminal_cmd(term, cli_cmd)

        # Launch
        try:
            log.info("Launching CLI: %s", " ".join(str(c) for c in full_cmd))
            log.info("Working directory: %s", cwd)
            self._process = subprocess.Popen(
                full_cmd,
                env=env,
                cwd=cwd,
                start_new_session=True,
                creationflags=_CREATION_FLAGS,
            )

            # Monitor in background thread to update menu when process exits
            t = threading.Thread(
                target=self._wait_for_exit,
                daemon=True,
                name="launcher-monitor",
            )
            t.start()

        except FileNotFoundError:
            msgbox(ctx, "WriterAgent",
                   "Failed to launch: terminal '%s' not found." % term)
        except Exception:
            log.exception("Failed to launch CLI")
            msgbox(ctx, "WriterAgent",
                   "Failed to launch AI CLI.")

    def _wait_for_exit(self):
        """Wait for CLI process to exit, then trigger menu update."""
        proc = self._process
        if proc is None:
            return
        try:
            proc.wait()
        except Exception:
            pass
        finally:
            self._process = None
            if hasattr(self._services, "events"):
                self._services.events.emit("menu:update")
