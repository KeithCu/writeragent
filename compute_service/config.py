# WriterAgent - Python Compute Service Configuration
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone settings for the Python compute service.

No UNO / writeragent.json dependency. Layered sources (later wins):

1. Secure defaults
2. Optional ``--config`` / ``PYTHON_COMPUTE_CONFIG`` JSON file
3. ``PYTHON_COMPUTE_*`` environment
4. Explicit CLI overrides (``--host``, ``--port``, ``--api-key-file``)

Secrets come from ``PYTHON_COMPUTE_API_KEY`` or a key file — never from argv.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

MIN_MAX_CODE_CHARS = 64
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"})
LOOPBACK_HOSTS = frozenset({"", "127.0.0.1", "::1", "localhost"})


def normalize_log_level(level: str) -> str:
    """Normalize log level strings (e.g. WARN -> WARNING)."""
    norm = (level or "").strip().upper()
    return "WARNING" if norm == "WARN" else norm


class ConfigError(ValueError):
    """Invalid compute-service configuration."""


def _as_path_tuple(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(os.pathsep) if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    raise ConfigError(f"ocr_allow_paths must be a list or {os.pathsep}-separated string")


def ocr_path_is_allowed(file_path: str, allow_prefixes: tuple[str, ...] | list[str]) -> bool:
    """True if *file_path* resolves to a file under one allowlisted prefix.

    Empty *allow_prefixes* denies every path (callers should use image_b64).
    """
    prefixes = [str(p).strip() for p in allow_prefixes if str(p).strip()]
    if not prefixes:
        return False
    try:
        resolved = os.path.realpath(os.path.expanduser(file_path.strip()))
    except (OSError, ValueError, TypeError):
        return False
    if not os.path.isfile(resolved):
        # Allowlist check still applies for missing files so we do not leak existence
        # via a different error before prefix match. Prefix-only:
        pass
    for raw in prefixes:
        try:
            base = os.path.realpath(os.path.expanduser(raw))
        except (OSError, ValueError):
            continue
        if resolved == base or resolved.startswith(base + os.sep):
            return True
    return False


@dataclass(frozen=True)
class ComputeSettings:
    """Immutable process settings for one compute-service instance."""

    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str = ""
    max_body_bytes: int = 32 * 1024 * 1024
    default_timeout_sec: int = 30
    max_timeout_sec: int = 600
    # Listener threads. Not a setting: one per formula worker and vision worker.
    threads: int = field(init=False, default=0)
    workers: int = 2
    worker_max_tasks: int = 500
    shared_kernel_ttl_sec: float = 3600.0
    idle_worker_ttl_sec: float = 3600.0
    ocr_workers: int = 0
    ocr_timeout_sec: int = 60
    ocr_max_tasks: int = 100
    max_code_chars: int = 262144
    ocr_allow_paths: tuple[str, ...] = ()
    log_level: str = "INFO"
    # Future: map authenticated principals to named profiles. Today always "default".
    default_principal: str = "default"

    def __post_init__(self) -> None:
        if self.workers is None:
            object.__setattr__(self, "workers", 2)
        # One listener thread per subprocess that can run a job.
        # GET /health shares this pool with /v1/execute. An execute holds its
        # thread for the whole lease and eval (up to max_timeout_sec), and
        # further executes blocked on a lease fill any spare threads, so a
        # liveness probe can sit in the queue under normal =PY() load. Adding
        # one thread does not fix that: health needs a path execute cannot
        # occupy. Left unchanged on purpose.
        object.__setattr__(self, "threads", self.workers + self.ocr_workers)
        object.__setattr__(self, "ocr_allow_paths", _as_path_tuple(self.ocr_allow_paths))
        object.__setattr__(self, "shared_kernel_ttl_sec", float(self.shared_kernel_ttl_sec))
        object.__setattr__(self, "idle_worker_ttl_sec", float(self.idle_worker_ttl_sec))
        object.__setattr__(self, "log_level", normalize_log_level(self.log_level))
        self.validate()

    @property
    def max_threads(self) -> int:
        """Alias for threads (HTTP listener capacity)."""
        return self.threads

    @property
    def max_workers(self) -> int:
        """Alias for workers (formula subprocess pool)."""
        return self.workers

    @property
    def auth_required(self) -> bool:
        return bool(self.api_key)

    @property
    def is_loopback_bind(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    def validate(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"Invalid port: {self.port}")
        if self.max_body_bytes < 1024:
            raise ConfigError("max_body_bytes must be at least 1024")
        if self.default_timeout_sec < 1 or self.max_timeout_sec < 1:
            raise ConfigError("timeout bounds must be >= 1")
        if self.default_timeout_sec > self.max_timeout_sec:
            raise ConfigError("default_timeout_sec cannot exceed max_timeout_sec")
        if self.threads < 1:
            raise ConfigError("threads must be >= 1")
        if self.workers < 1:
            raise ConfigError("workers must be >= 1")
        if self.worker_max_tasks < 1:
            raise ConfigError("worker_max_tasks must be >= 1")
        if self.ocr_workers < 0:
            raise ConfigError("ocr_workers must be >= 0")
        if self.ocr_timeout_sec < 1:
            raise ConfigError("ocr_timeout_sec must be >= 1")
        if self.ocr_max_tasks < 1:
            raise ConfigError("ocr_max_tasks must be >= 1")
        if self.max_code_chars < MIN_MAX_CODE_CHARS:
            raise ConfigError(f"max_code_chars must be >= {MIN_MAX_CODE_CHARS}")
        if self.shared_kernel_ttl_sec < 0:
            raise ConfigError("shared_kernel_ttl_sec must be >= 0")
        if self.idle_worker_ttl_sec < 0:
            raise ConfigError("idle_worker_ttl_sec must be >= 0")
        if self.log_level.upper() not in VALID_LOG_LEVELS:
            raise ConfigError(f"Invalid log_level: {self.log_level!r} (must be one of {sorted(VALID_LOG_LEVELS)})")
        # No API key ⇒ no auth (dev/test). Verification runs only when a key is set.


DEFAULT_SETTINGS = ComputeSettings()


def _as_int(value: Any, *, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer for {field}: {value!r}") from exc


def _as_float(value: Any, *, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be a number: {value!r}") from exc


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
    # Do NOT call text.strip() — that would silently mangle keys with leading/trailing spaces.
    if not text:
        raise ConfigError(f"api_key_file {key_path} is empty")
    return text


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
    """Accept flat keys or nested ``listen`` / ``auth`` / ``limits`` / ``ocr`` sections."""
    out: dict[str, Any] = {}
    listen = raw.get("listen")
    if isinstance(listen, Mapping):
        if "host" in listen:
            out["host"] = listen["host"]
        if "port" in listen:
            out["port"] = listen["port"]
    # A raw api_key in the file used to be dropped and the process started
    # with auth off (the log line "auth=no" is easy to miss). Refuse it so
    # the operator uses a key file or PYTHON_COMPUTE_API_KEY. Do not accept
    # the raw key.
    auth = raw.get("auth")
    if "api_key" in raw or (isinstance(auth, Mapping) and "api_key" in auth):
        raise ConfigError("Do not put api_key in the JSON config. Set PYTHON_COMPUTE_API_KEY or auth.api_key_file.")
    if isinstance(auth, Mapping):
        if "api_key_file" in auth:
            out["api_key_file"] = auth["api_key_file"]
    limits = raw.get("limits")
    if isinstance(limits, Mapping):
        for key in (
            "max_body_bytes",
            "default_timeout_sec",
            "max_timeout_sec",
            "workers",
            "max_workers",
            "worker_max_tasks",
            "shared_kernel_ttl_sec",
            "session_ttl_sec",
            "idle_worker_ttl_sec",
            "max_code_chars",
        ):
            if key in limits:
                out[key] = limits[key]
    ocr_cfg = raw.get("ocr")
    if isinstance(ocr_cfg, Mapping):
        for ocr_key, out_key in (
            ("workers", "ocr_workers"),
            ("timeout_sec", "ocr_timeout_sec"),
            ("max_tasks", "ocr_max_tasks"),
            ("allow_paths", "ocr_allow_paths"),
        ):
            if ocr_key in ocr_cfg:
                out[out_key] = ocr_cfg[ocr_key]
    logging_cfg = raw.get("logging")
    if isinstance(logging_cfg, Mapping):
        if "log_level" in logging_cfg:
            out["log_level"] = logging_cfg["log_level"]
        elif "level" in logging_cfg:
            out["log_level"] = logging_cfg["level"]

    for key in (
        "host",
        "port",
        "api_key_file",
        "max_body_bytes",
        "default_timeout_sec",
        "max_timeout_sec",
        "workers",
        "max_workers",
        "worker_max_tasks",
        "shared_kernel_ttl_sec",
        "session_ttl_sec",
        "idle_worker_ttl_sec",
        "ocr_workers",
        "ocr_timeout_sec",
        "ocr_max_tasks",
        "ocr_allow_paths",
        "max_code_chars",
        "log_level",
    ):
        if key in raw and key not in out:
            out[key] = raw[key]

    # Resolve aliases once after collecting all nested and flat keys
    if "session_ttl_sec" in out:
        out.setdefault("shared_kernel_ttl_sec", out.pop("session_ttl_sec"))
    if "max_workers" in out:
        out.setdefault("workers", out.pop("max_workers"))
    return out


def load_settings(
    *,
    config_path: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    workers: int | None = None,
    max_workers: int | None = None,
    worker_max_tasks: int | None = None,
    ocr_workers: int | None = None,
    ocr_timeout_sec: int | None = None,
    ocr_max_tasks: int | None = None,
    api_key_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ComputeSettings:
    """Resolve settings from defaults → JSON → env → explicit CLI overrides."""
    env = os.environ if environ is None else environ

    values: dict[str, Any] = {}

    resolved_config = config_path or env.get("PYTHON_COMPUTE_CONFIG") or ""
    if resolved_config:
        values.update(_flatten_config_json(_load_json_file(resolved_config)))

    # Environment settings.
    if env.get("PYTHON_COMPUTE_HOST"):
        values["host"] = env["PYTHON_COMPUTE_HOST"]

    if env.get("PYTHON_COMPUTE_PORT"):
        values["port"] = env["PYTHON_COMPUTE_PORT"]

    if env.get("PYTHON_COMPUTE_MAX_BODY_BYTES"):
        values["max_body_bytes"] = env["PYTHON_COMPUTE_MAX_BODY_BYTES"]
    if env.get("PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC"):
        values["default_timeout_sec"] = env["PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC"]
    if env.get("PYTHON_COMPUTE_MAX_TIMEOUT_SEC"):
        values["max_timeout_sec"] = env["PYTHON_COMPUTE_MAX_TIMEOUT_SEC"]

    if env.get("PYTHON_COMPUTE_WORKERS"):
        values["workers"] = env["PYTHON_COMPUTE_WORKERS"]
    elif env.get("PYTHON_COMPUTE_MAX_WORKERS"):
        values["workers"] = env["PYTHON_COMPUTE_MAX_WORKERS"]

    if env.get("PYTHON_COMPUTE_WORKER_MAX_TASKS"):
        values["worker_max_tasks"] = env["PYTHON_COMPUTE_WORKER_MAX_TASKS"]
    if env.get("PYTHON_COMPUTE_SHARED_KERNEL_TTL_SEC"):
        values["shared_kernel_ttl_sec"] = env["PYTHON_COMPUTE_SHARED_KERNEL_TTL_SEC"]
    elif env.get("PYTHON_COMPUTE_SESSION_TTL_SEC"):
        values["shared_kernel_ttl_sec"] = env["PYTHON_COMPUTE_SESSION_TTL_SEC"]
    if env.get("PYTHON_COMPUTE_IDLE_WORKER_TTL_SEC"):
        values["idle_worker_ttl_sec"] = env["PYTHON_COMPUTE_IDLE_WORKER_TTL_SEC"]
    if env.get("PYTHON_COMPUTE_OCR_WORKERS"):
        values["ocr_workers"] = env["PYTHON_COMPUTE_OCR_WORKERS"]
    if env.get("PYTHON_COMPUTE_OCR_TIMEOUT_SEC"):
        values["ocr_timeout_sec"] = env["PYTHON_COMPUTE_OCR_TIMEOUT_SEC"]
    if env.get("PYTHON_COMPUTE_OCR_MAX_TASKS"):
        values["ocr_max_tasks"] = env["PYTHON_COMPUTE_OCR_MAX_TASKS"]
    if env.get("PYTHON_COMPUTE_MAX_CODE_CHARS"):
        values["max_code_chars"] = env["PYTHON_COMPUTE_MAX_CODE_CHARS"]
    if env.get("PYTHON_COMPUTE_OCR_ALLOW_PATHS"):
        values["ocr_allow_paths"] = env["PYTHON_COMPUTE_OCR_ALLOW_PATHS"]
    if env.get("PYTHON_COMPUTE_LOG_LEVEL"):
        values["log_level"] = env["PYTHON_COMPUTE_LOG_LEVEL"]

    env_key = (env.get("PYTHON_COMPUTE_API_KEY") or "").strip()
    env_key_file = (env.get("PYTHON_COMPUTE_API_KEY_FILE") or "").strip()
    json_key_file = str(values.pop("api_key_file", "") or "").strip()

    # Explicit CLI overrides last.
    if host is not None:
        values["host"] = host
    if port is not None:
        values["port"] = port

    if workers is not None:
        values["workers"] = workers
    elif max_workers is not None:
        values["workers"] = max_workers

    if worker_max_tasks is not None:
        values["worker_max_tasks"] = worker_max_tasks
    if ocr_workers is not None:
        values["ocr_workers"] = ocr_workers
    if ocr_timeout_sec is not None:
        values["ocr_timeout_sec"] = ocr_timeout_sec
    if ocr_max_tasks is not None:
        values["ocr_max_tasks"] = ocr_max_tasks

    # Secret resolution: CLI key-file > env key > env key-file > JSON key-file.
    chosen_key_file = api_key_file or env_key_file or json_key_file or None
    if api_key_file:
        values["api_key"] = _read_key_file(api_key_file)
    elif env_key:
        values["api_key"] = env_key
    elif chosen_key_file:
        values["api_key"] = _read_key_file(chosen_key_file)

    for int_field in ("port", "max_body_bytes", "default_timeout_sec", "max_timeout_sec", "workers", "worker_max_tasks", "ocr_workers", "ocr_timeout_sec", "ocr_max_tasks", "max_code_chars"):
        if int_field in values:
            values[int_field] = _as_int(values[int_field], field=int_field)

    for float_field in ("shared_kernel_ttl_sec", "idle_worker_ttl_sec"):
        if float_field in values:
            values[float_field] = _as_float(values[float_field], field=float_field)

    if "ocr_allow_paths" in values:
        values["ocr_allow_paths"] = _as_path_tuple(values["ocr_allow_paths"])

    if "host" in values:
        if not values["host"]:
            values.pop("host")
        else:
            values["host"] = str(values["host"])

    if "log_level" in values:
        if not values["log_level"]:
            values.pop("log_level")
        else:
            values["log_level"] = normalize_log_level(str(values["log_level"]))

    return ComputeSettings(**values)
