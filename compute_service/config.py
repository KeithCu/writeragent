# WriterAgent - Python Compute Service Configuration
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone settings for the Python compute service.

No UNO / writeragent.json dependency. Layered sources (later wins):

1. Secure defaults
2. Optional ``--config`` / ``PYTHON_COMPUTE_CONFIG`` JSON file
3. ``PYTHON_COMPUTE_*`` environment (plus legacy ``HOST`` / ``PORT``)
4. Explicit CLI overrides (``--host``, ``--port``, ``--api-key-file``)

Secrets come from ``PYTHON_COMPUTE_API_KEY`` or a key file — never from argv.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024
_DEFAULT_TIMEOUT_SEC = 30
_MAX_TIMEOUT_SEC = 600

_LOOPBACK_HOSTS = frozenset({"", "127.0.0.1", "::1", "localhost"})


class ConfigError(ValueError):
    """Invalid compute-service configuration."""


@dataclass(frozen=True)
class ComputeSettings:
    """Immutable process settings for one compute-service instance."""

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    api_key: str = ""
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES
    default_timeout_sec: int = _DEFAULT_TIMEOUT_SEC
    max_timeout_sec: int = _MAX_TIMEOUT_SEC
    # Future: map authenticated principals to named profiles. Today always "default".
    default_principal: str = "default"

    @property
    def auth_required(self) -> bool:
        return bool(self.api_key)

    @property
    def is_loopback_bind(self) -> bool:
        return self.host in _LOOPBACK_HOSTS

    def validate(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"Invalid port: {self.port}")
        if self.max_body_bytes < 1024:
            raise ConfigError("max_body_bytes must be at least 1024")
        if self.default_timeout_sec < 1 or self.max_timeout_sec < 1:
            raise ConfigError("timeout bounds must be >= 1")
        if self.default_timeout_sec > self.max_timeout_sec:
            raise ConfigError("default_timeout_sec cannot exceed max_timeout_sec")
        # No API key ⇒ no auth (dev/test). Verification runs only when a key is set.


def _as_int(value: Any, *, field: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer for {field}: {value!r}") from exc


def _read_key_file(path: str | Path) -> str:
    key_path = Path(path).expanduser()
    try:
        text = key_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read api_key_file {key_path}: {exc}") from exc
    # Strip one trailing newline only if present; keep interior whitespace.
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n") or text.endswith("\r"):
        text = text[:-1]
    key = text.strip()
    if not key:
        raise ConfigError(f"api_key_file {key_path} is empty")
    return key


def _load_json_file(path: str | Path) -> dict[str, Any]:
    cfg_path = Path(path).expanduser()
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {cfg_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in config file {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {cfg_path} must contain a JSON object")
    return raw


def _flatten_config_json(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Accept flat keys or nested ``listen`` / ``auth`` / ``limits`` sections."""
    out: dict[str, Any] = {}
    listen = raw.get("listen")
    if isinstance(listen, Mapping):
        if "host" in listen:
            out["host"] = listen["host"]
        if "port" in listen:
            out["port"] = listen["port"]
    auth = raw.get("auth")
    if isinstance(auth, Mapping):
        if "api_key_file" in auth:
            out["api_key_file"] = auth["api_key_file"]
        # Deliberately ignore raw "api_key" in JSON files — prefer env / key file.
    limits = raw.get("limits")
    if isinstance(limits, Mapping):
        if "max_body_bytes" in limits:
            out["max_body_bytes"] = limits["max_body_bytes"]
        if "default_timeout_sec" in limits:
            out["default_timeout_sec"] = limits["default_timeout_sec"]
        if "max_timeout_sec" in limits:
            out["max_timeout_sec"] = limits["max_timeout_sec"]

    for key in (
        "host",
        "port",
        "api_key_file",
        "max_body_bytes",
        "default_timeout_sec",
        "max_timeout_sec",
    ):
        if key in raw and key not in out:
            out[key] = raw[key]
    return out


def load_settings(
    *,
    config_path: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    api_key_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ComputeSettings:
    """Resolve settings from defaults → JSON → env → explicit CLI overrides."""
    env = os.environ if environ is None else environ

    values: dict[str, Any] = {
        "host": _DEFAULT_HOST,
        "port": _DEFAULT_PORT,
        "api_key": "",
        "max_body_bytes": _DEFAULT_MAX_BODY_BYTES,
        "default_timeout_sec": _DEFAULT_TIMEOUT_SEC,
        "max_timeout_sec": _MAX_TIMEOUT_SEC,
    }

    resolved_config = config_path or env.get("PYTHON_COMPUTE_CONFIG") or ""
    if resolved_config:
        values.update(_flatten_config_json(_load_json_file(resolved_config)))

    # Environment (legacy HOST/PORT kept for existing Docker / start scripts).
    if env.get("PYTHON_COMPUTE_HOST"):
        values["host"] = env["PYTHON_COMPUTE_HOST"]
    elif env.get("HOST"):
        values["host"] = env["HOST"]

    if env.get("PYTHON_COMPUTE_PORT"):
        values["port"] = env["PYTHON_COMPUTE_PORT"]
    elif env.get("PORT"):
        values["port"] = env["PORT"]

    if env.get("PYTHON_COMPUTE_MAX_BODY_BYTES"):
        values["max_body_bytes"] = env["PYTHON_COMPUTE_MAX_BODY_BYTES"]
    if env.get("PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC"):
        values["default_timeout_sec"] = env["PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC"]
    if env.get("PYTHON_COMPUTE_MAX_TIMEOUT_SEC"):
        values["max_timeout_sec"] = env["PYTHON_COMPUTE_MAX_TIMEOUT_SEC"]

    env_key = (env.get("PYTHON_COMPUTE_API_KEY") or "").strip()
    env_key_file = (env.get("PYTHON_COMPUTE_API_KEY_FILE") or "").strip()
    json_key_file = str(values.pop("api_key_file", "") or "").strip()

    # Explicit CLI overrides last.
    if host is not None:
        values["host"] = host
    if port is not None:
        values["port"] = port

    # Secret resolution: CLI key-file > env key > env key-file > JSON key-file.
    api_key = ""
    chosen_key_file = api_key_file or env_key_file or json_key_file or None
    if api_key_file:
        api_key = _read_key_file(api_key_file)
    elif env_key:
        api_key = env_key
    elif chosen_key_file:
        api_key = _read_key_file(chosen_key_file)

    settings = ComputeSettings(
        host=str(values["host"] or _DEFAULT_HOST),
        port=_as_int(values["port"], field="port", default=_DEFAULT_PORT),
        api_key=api_key,
        max_body_bytes=_as_int(
            values["max_body_bytes"], field="max_body_bytes", default=_DEFAULT_MAX_BODY_BYTES
        ),
        default_timeout_sec=_as_int(
            values["default_timeout_sec"],
            field="default_timeout_sec",
            default=_DEFAULT_TIMEOUT_SEC,
        ),
        max_timeout_sec=_as_int(
            values["max_timeout_sec"], field="max_timeout_sec", default=_MAX_TIMEOUT_SEC
        ),
    )
    settings.validate()
    return settings
