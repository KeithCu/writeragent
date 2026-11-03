# WriterAgent - subprocess environment scrubbing (framework; no application imports)
"""Strip credentials and LibreOffice Python overrides from child process environments."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Container

from plugin.framework.deal_shim import (
    DEAL_MAX_ARGV,
    DEAL_MAX_TOKEN,
    deal,
    inverse_ensure,
)

# --- Subprocess environment ---

_BLOCKED_ENV_TOKENS = frozenset(
    {
        "KEY",
        "KEYS",
        "TOKEN",
        "TOKENS",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWORDS",
        "PASSWD",
        "PASSPHRASE",
        "PASSPHRASES",
        "PAT",
        "PWD",
        "CREDENTIAL",
        "CREDENTIALS",
        "AUTH",
        "AUTHORIZATION",
        "WEBHOOK",
        "WEBHOOKS",
        "KEYRING",
        "KEYFILE",
        "KEYSTORE",
        "KEYTAB",
    }
)

_GLUED_CREDENTIAL_TOKENS = frozenset(
    {
        "APIKEY",
        "APITOKEN",
        "SECRETKEY",
        "ACCESSKEY",
        "ACCESSKEYID",
        "PRIVATEKEY",
        "AUTHKEY",
        "AUTHTOKEN",
        "APPKEY",
        "APPTOKEN",
        "APISECRET",
    }
)

# XAUTHORITY contains AUTH but is the X11 cookie path, not a credential name.
# PWD is the shell cwd, not a password.
_ENV_CREDENTIAL_ALLOW = frozenset({"XAUTHORITY", "PWD"})

# LibreOffice sets PYTHONHOME/PYTHONPATH to its bundled stdlib; letting these
# leak into a venv subprocess causes SRE module mismatch and import failures.
# LD_PRELOAD, LD_AUDIT, and DYLD_INSERT_LIBRARIES run code before the harness.
# PYTHONSTARTUP, PYTHONUSERBASE, PYTHONBREAKPOINT, PYTHONINSPECT alter python execution.
# DATABASE_URL is a common connection string containing database credentials.
# WRITERAGENT_COMPUTE_WORKER is compute-service only; venv workers support bidirectional tool calls.
_BLOCKED_ENV_EXACT = {
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONBREAKPOINT",
    "PYTHONINSPECT",
    "PYTHONEXECUTABLE",
    "PYTHONNOUSERSITE",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
    "DATABASE_URL",
    "WRITERAGENT_COMPUTE_WORKER",
}


# scrub_subprocess_env: keep pytest wide.
_DEAL_SCRUB_DICT = DEAL_MAX_ARGV
_DEAL_SCRUB_KEY = DEAL_MAX_TOKEN
_DEAL_SCRUB_VAL = DEAL_MAX_ARGV


def _deal_scrub_env_ok_pytest(base: object) -> bool:
    # os.environ values (PATH) exceed DEAL_MAX_ARGV and names can exceed
    # a token. The old cap raised PreContractError before secrets were
    # dropped. Values must stay str so the post (str→str) holds.
    # CrossHair keeps the one-entry domain.
    if base is None:
        return True
    return isinstance(base, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in base.items())


_deal_scrub_env_ok = _deal_scrub_env_ok_pytest


def _env_name_is_credential(name: str) -> bool:
    """True when an env name contains a glued credential substring or matches blocked tokens on _ segments.

    After the allowlist and DATABASE_URL / DSN checks, names matching any
    glued credential compound substring (_GLUED_CREDENTIAL_TOKENS) are
    credentials (e.g. OPENAIAPIKEY, OPENAI_APIKEY, OPENAI-APIKEY).
    Short tokens in _BLOCKED_ENV_TOKENS still match only a non-empty _ or -
    segment, keeping PWD, XAUTHORITY, MONKEY, TURKEY, SECRETARY, KEYWORD,
    KEYBOARD, KEYBOARD_LAYOUT, and TOKENIZERS_PARALLELISM allowed.
    """
    nu = name.upper()
    if nu in _ENV_CREDENTIAL_ALLOW:
        return False
    if nu == "DATABASE_URL" or nu == "DSN" or nu.endswith("_DSN"):
        return True
    # Bugfix: segment matching left OPENAIAPIKEY (no _ or -) in the child env
    # because token splitting only inspected delimited tokens. Substring
    # matching the glued compounds fixes this; the glued compounds are specific
    # enough that a substring does not hit MONKEY, SECRETARY, KEYWORD, or
    # TOKENIZERS_PARALLELISM.
    if any(glued in nu for glued in _GLUED_CREDENTIAL_TOKENS):
        return True
    tokens = [part for part in nu.replace("-", "_").split("_") if part]
    return any(token in _BLOCKED_ENV_TOKENS for token in tokens)


@deal.pre(lambda base, keep=None: _deal_scrub_env_ok(base))
@deal.post(lambda result: isinstance(result, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in result.items()))
@inverse_ensure(lambda base, result, keep=None: all(k.upper() not in _BLOCKED_ENV_EXACT or (keep is not None and k in keep) for k in result))
@inverse_ensure(lambda base, result, keep=None: all(not _env_name_is_credential(k) for k in result))
@deal.ensure(
    lambda base, result, keep=None: base is None
    or (
        result.get("PYTHONIOENCODING") == "utf-8"
        and result.get("PYTHONUTF8") == "1"
        and result.get("PYTHONDONTWRITEBYTECODE") == "1"
    )
)
def scrub_subprocess_env(
    base: dict[str, str] | None,
    *,
    keep: Container[str] | None = None,
) -> dict[str, str]:
    """Drop likely-secret vars and LO Python overrides from the environment passed to venv Python.

    Only ``base is None`` returns {}. An empty dict still receives the UTF-8
    and bytecode overrides. Returning {} for {} skipped those overrides.
    """
    if base is None:
        return {}
    out: dict[str, str] = {}
    for k, v in base.items():
        if _env_name_is_credential(k):
            continue
        ku = k.upper()
        if ku in _BLOCKED_ENV_EXACT and (keep is None or k not in keep):
            continue
        out[k] = v
    # setdefault kept whatever the parent already had. Callers pass
    # dict(os.environ) (venv worker, editor host, compute worker). A latin-1
    # PYTHONIOENCODING, PYTHONUTF8=0, or PYTHONDONTWRITEBYTECODE=0 then failed
    # the ensure in deal builds and spawned the child with that encoding once
    # release builds strip deal. Force the values the postcondition requires.
    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONUTF8"] = "1"
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    # Child processes should log to the same writeragent_debug.log. Read the
    # path at call time (logging may not be initialized at import). Use the
    # public getter; a private import plus bare ``except Exception`` hid
    # failures other than a missing logging module.
    try:
        from plugin.framework.logging import get_debug_log_path
    except ImportError:
        return out
    debug_log_path = get_debug_log_path()
    if debug_log_path:
        out["WRITERAGENT_DEBUG_LOG_PATH"] = debug_log_path
    return out
