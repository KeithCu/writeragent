# WriterAgent - AI Writing Assistant for LibreOffice
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
#
# The macOS Keychain backend (_MacKeychain) is ported from keyring
# (keyring/backends/macOS/api.py, keyring 25.7.0):
# Copyright Jason R. Coombs, keyring, MIT License.
"""OS keystore integration for WriterAgent secrets.

Secrets (LLM API keys and ``widget: password`` config values) are stored in
the platform's native credential store under service ``"WriterAgent"`` when
available, and replaced in ``writeragent.json`` with a reference token of the
form ``keystore:<8 hex>`` (``REF_PREFIX``). When no OS store is available or
a write fails, secrets remain in plain text in ``writeragent.json``.

Concurrency: all ``set_secret`` and ``delete_secret`` calls run inside
``config.save()`` while ``_config_write_lock`` is held. ``_cache_lock`` is a
leaf lock that guards ``_cache`` only and is never held across an OS call.
Setters write the OS first and overwrite ``_cache[name]``; readers call the OS
without ``_cache_lock`` on a miss and merge with ``_cache.setdefault(name,
value)`` so a stale read never overwrites a newer write or delete.

Troubleshooting unreadable stores:
- Linux (Secret Service / gnome-keyring / Plasma ksecretd): unlock the login
  keyring or answer the unlock prompt; a cancelled prompt is not cached—reads
  within ``LOCKED_RETRY_S`` return ``None`` without prompting, and the next read
  after that prompts again.
- macOS (Keychain): click Allow / Always Allow on the Keychain access prompt
  (a LibreOffice update changes the code signature and may re-prompt).
- Windows (Credential Manager): verify the ``WriterAgent/<name>`` generic
  credential exists under the current Windows user account.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol, final

from plugin.framework.errors import WriterAgentException

if TYPE_CHECKING:
    from collections.abc import Generator

log = logging.getLogger(__name__)

SERVICE = "WriterAgent"
REF_PREFIX = "keystore:"

_UNSET = object()
_detected: bool = False
_resolved: _KeystoreBackend | None = None
_init_lock = threading.Lock()
_cache: dict[str, str | None] = {}
_cache_lock = threading.Lock()
# Latched after any set_secret failure so a cancelled Linux unlock prompt
# never re-prompts once per slot during a single migration batch.
_writes_failed: bool = False
_read_failures_logged: set[str] = set()
# Names whose last read hit a locked keyring (or a cancelled unlock prompt),
# with the monotonic time of that read. Not cached, so a later read can prompt
# again, but reads within LOCKED_RETRY_S return None without prompting:
# Settings reads the same key several times while opening
# (settings_dialog._get_core_field_specs, dialog_views._schedule_initial_models_fetch,
# and the TTS voice context), and background callers such as the grammar checker
# read on every request. A cancelled prompt must not come right back for each of
# them; Send itself reads once on its LLM worker.
_locked: dict[str, float] = {}
LOCKED_RETRY_S = 5.0


class KeystoreError(WriterAgentException):
    """Raised by platform keystore backends on OS store errors."""

    code: str = "KEYSTORE_ERROR"


class KeystoreLocked(KeystoreError):
    """The item exists but the store is locked (or the unlock prompt was cancelled)."""

    code: str = "KEYSTORE_LOCKED"


class _KeystoreBackend(Protocol):
    label: str

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


@final
class _GError(ctypes.Structure):
    _fields_ = [
        ("domain", ctypes.c_uint32),
        ("code", ctypes.c_int),
        ("message", ctypes.c_char_p),
    ]


@final
class _SecretSchemaAttribute(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("type", ctypes.c_int),
    ]


@final
class _SecretSchema(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("flags", ctypes.c_int),
        ("attributes", _SecretSchemaAttribute * 32),
        ("reserved", ctypes.c_int),
        ("reserved1", ctypes.c_void_p),
        ("reserved2", ctypes.c_void_p),
        ("reserved3", ctypes.c_void_p),
        ("reserved4", ctypes.c_void_p),
        ("reserved5", ctypes.c_void_p),
        ("reserved6", ctypes.c_void_p),
        ("reserved7", ctypes.c_void_p),
    ]


@final
class _LibSecret:
    """Linux Secret Service backend using ctypes on libsecret-1 and libglib-2.0."""

    label = "Secret Service"

    def __init__(self) -> None:
        libsecret = ctypes.CDLL("libsecret-1.so.0")
        libglib = ctypes.CDLL("libglib-2.0.so.0")

        self._g_str_hash = libglib.g_str_hash
        self._g_str_equal = libglib.g_str_equal

        self._g_hash_table_new = libglib.g_hash_table_new
        self._g_hash_table_new.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._g_hash_table_new.restype = ctypes.c_void_p

        self._g_hash_table_insert = libglib.g_hash_table_insert
        self._g_hash_table_insert.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self._g_hash_table_insert.restype = ctypes.c_int

        self._g_hash_table_unref = libglib.g_hash_table_unref
        self._g_hash_table_unref.argtypes = [ctypes.c_void_p]
        self._g_hash_table_unref.restype = None

        self._g_error_free = libglib.g_error_free
        self._g_error_free.argtypes = [ctypes.POINTER(_GError)]
        self._g_error_free.restype = None

        self._lookupv_sync = libsecret.secret_password_lookupv_sync
        self._lookupv_sync.argtypes = [
            ctypes.POINTER(_SecretSchema),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(_GError)),
        ]
        self._lookupv_sync.restype = ctypes.c_void_p

        self._storev_sync = libsecret.secret_password_storev_sync
        self._storev_sync.argtypes = [
            ctypes.POINTER(_SecretSchema),
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(_GError)),
        ]
        self._storev_sync.restype = ctypes.c_int

        self._clearv_sync = libsecret.secret_password_clearv_sync
        self._clearv_sync.argtypes = [
            ctypes.POINTER(_SecretSchema),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(_GError)),
        ]
        self._clearv_sync.restype = ctypes.c_int

        # Non-prompting search (no SECRET_SEARCH_UNLOCK) used to tell a missing
        # item from a locked one after a lookup returns NULL.
        # secret_password_searchv_sync was added in libsecret 0.19.0
        # (Secret-1.gir: password_searchv_sync version="0.19.0"). On older
        # distributions the store still works; we just cannot tell a locked
        # item from a missing one, so a cancelled prompt reads as no key.
        self._searchv_sync = getattr(libsecret, "secret_password_searchv_sync", None)
        if self._searchv_sync is not None:
            self._searchv_sync.argtypes = [
                ctypes.POINTER(_SecretSchema),
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.POINTER(_GError)),
            ]
            self._searchv_sync.restype = ctypes.c_void_p

        libgobject = ctypes.CDLL("libgobject-2.0.so.0")
        self._g_object_unref = libgobject.g_object_unref
        self._g_list_free_full = libglib.g_list_free_full
        self._g_list_free_full.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._g_list_free_full.restype = None

        self._secret_password_free = libsecret.secret_password_free
        self._secret_password_free.argtypes = [ctypes.c_void_p]
        self._secret_password_free.restype = None

        # SECRET_SCHEMA_DONT_MATCH_NAME (2) lets us share the generic schema
        # across Secret Service providers (gnome-keyring, Plasma ksecretd).
        self._schema = _SecretSchema(name=b"org.freedesktop.Secret.Generic", flags=2)
        self._schema.attributes[0] = _SecretSchemaAttribute(name=b"service", type=0)
        self._schema.attributes[1] = _SecretSchemaAttribute(name=b"username", type=0)
        self._schema.attributes[2] = _SecretSchemaAttribute(name=None, type=0)

        # Probe once with a non-matching name so missing D-Bus/Secret Service
        # fails fast without triggering an unlock prompt.
        self.get("__writeragent_probe__")

    def _raise_gerror(self, err_ptr: Any) -> None:
        if err_ptr:
            try:
                msg = (err_ptr.contents.message or b"libsecret error").decode("utf-8", errors="replace")
            finally:
                self._g_error_free(err_ptr)
            raise KeystoreError(msg)

    @contextmanager
    def _attrs(self, name: str) -> Generator[ctypes.c_void_p]:
        """Yield the ``service``/``username`` attribute table for *name*, then unref it.

        The table stores raw pointers into *keepalive*, which this frame holds until the unref.
        """
        table = self._g_hash_table_new(self._g_str_hash, self._g_str_equal)
        keepalive = [
            ctypes.c_char_p(b"service"),
            ctypes.c_char_p(SERVICE.encode("utf-8")),
            ctypes.c_char_p(b"username"),
            ctypes.c_char_p(name.encode("utf-8")),
        ]
        self._g_hash_table_insert(table, ctypes.cast(keepalive[0], ctypes.c_void_p), ctypes.cast(keepalive[1], ctypes.c_void_p))
        self._g_hash_table_insert(table, ctypes.cast(keepalive[2], ctypes.c_void_p), ctypes.cast(keepalive[3], ctypes.c_void_p))
        try:
            yield table
        finally:
            self._g_hash_table_unref(table)

    def get(self, name: str) -> str | None:
        err = ctypes.POINTER(_GError)()
        with self._attrs(name) as table:
            ptr = self._lookupv_sync(ctypes.byref(self._schema), table, None, ctypes.byref(err))
            self._raise_gerror(err)
            if not ptr:
                if self._item_exists(table):
                    raise KeystoreLocked(f"Secret {name!r} is in a locked keyring")
                return None
            try:
                return ctypes.string_at(ptr).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise KeystoreError(f"Invalid UTF-8 in secret {name!r}: {exc}") from exc
            finally:
                self._secret_password_free(ptr)

    def _item_exists(self, table: Any) -> bool:
        """True when a matching item exists, locked or not, without prompting."""
        if self._searchv_sync is None:
            return False
        err = ctypes.POINTER(_GError)()
        found = self._searchv_sync(ctypes.byref(self._schema), table, 0, None, ctypes.byref(err))
        if err:
            self._g_error_free(err)
        if not found:
            return False
        self._g_list_free_full(found, ctypes.cast(self._g_object_unref, ctypes.c_void_p))
        return True

    def set(self, name: str, value: str) -> None:
        err = ctypes.POINTER(_GError)()
        label_bytes = f"{SERVICE}: {name}".encode("utf-8")
        value_bytes = value.encode("utf-8")
        with self._attrs(name) as table:
            ok = self._storev_sync(
                ctypes.byref(self._schema),
                table,
                None,
                label_bytes,
                value_bytes,
                None,
                ctypes.byref(err),
            )
            self._raise_gerror(err)
            if not ok:
                raise KeystoreError("secret_password_storev_sync returned FALSE")

    def delete(self, name: str) -> None:
        err = ctypes.POINTER(_GError)()
        with self._attrs(name) as table:
            self._clearv_sync(ctypes.byref(self._schema), table, None, ctypes.byref(err))
            self._raise_gerror(err)


@final
class _WinCredentials:
    """Windows Credential Manager backend using ctypes on advapi32."""

    label = "Credential Manager"
    _cred_struct: Any
    _cred_read: Any
    _cred_write: Any
    _cred_delete: Any
    _cred_free: Any
    _get_last_error: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise KeystoreError("Windows Credential Manager is only available on win32")
        from ctypes import wintypes

        @final
        class _CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._get_last_error = ctypes.get_last_error

        self._cred_struct = _CREDENTIALW
        self._cred_read = advapi32.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._cred_read.restype = wintypes.BOOL

        self._cred_write = advapi32.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL

        self._cred_delete = advapi32.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL

        self._cred_free = advapi32.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    def get(self, name: str) -> str | None:
        pcred: Any = ctypes.POINTER(self._cred_struct)()
        if not self._cred_read(f"{SERVICE}/{name}", 1, 0, ctypes.byref(pcred)):
            err = int(self._get_last_error())
            if err == 1168:  # ERROR_NOT_FOUND
                return None
            raise KeystoreError(f"CredReadW failed (error {err})")
        try:
            cred = pcred.contents
            if not cred.CredentialBlob or cred.CredentialBlobSize == 0:
                return ""
            try:
                return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-16-le")
            except UnicodeDecodeError as exc:
                raise KeystoreError(f"Invalid UTF-16-LE in secret {name!r}: {exc}") from exc
        finally:
            self._cred_free(pcred)

    def set(self, name: str, value: str) -> None:
        blob = value.encode("utf-16-le")
        blob_buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else None
        cred = self._cred_struct(
            Flags=0,
            Type=1,  # CRED_TYPE_GENERIC
            TargetName=f"{SERVICE}/{name}",
            Comment=None,
            CredentialBlobSize=len(blob),
            CredentialBlob=blob_buf,
            Persist=3,  # CRED_PERSIST_ENTERPRISE: roams with %APPDATA% like writeragent.json
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=SERVICE,  # CredWriteW caps UserName at 513 chars; endpoint URLs can be longer
        )
        if not self._cred_write(ctypes.byref(cred), 0):
            err = int(self._get_last_error())
            raise KeystoreError(f"CredWriteW failed (error {err})")

    def delete(self, name: str) -> None:
        if not self._cred_delete(f"{SERVICE}/{name}", 1, 0):
            err = int(self._get_last_error())
            if err != 1168:  # ERROR_NOT_FOUND
                raise KeystoreError(f"CredDeleteW failed (error {err})")


@final
class _MacKeychain:
    """macOS Keychain backend ported from keyring (keyring/backends/macOS/api.py)."""

    label = "Keychain"

    def __init__(self) -> None:
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._sec = sec

        os_status = ctypes.c_int32

        self._cf_dict_create = cf.CFDictionaryCreate
        self._cf_dict_create.restype = ctypes.c_void_p
        self._cf_dict_create.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        self._cf_str_create = cf.CFStringCreateWithCString
        self._cf_str_create.restype = ctypes.c_void_p
        self._cf_str_create.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]

        self._cf_num_create = cf.CFNumberCreate
        self._cf_num_create.restype = ctypes.c_void_p
        self._cf_num_create.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]

        self._sec_item_add = sec.SecItemAdd
        self._sec_item_add.restype = os_status
        self._sec_item_add.argtypes = (ctypes.c_void_p, ctypes.c_void_p)

        self._sec_item_copy_matching = sec.SecItemCopyMatching
        self._sec_item_copy_matching.restype = os_status
        self._sec_item_copy_matching.argtypes = (ctypes.c_void_p, ctypes.c_void_p)

        self._sec_item_delete = sec.SecItemDelete
        self._sec_item_delete.restype = os_status
        self._sec_item_delete.argtypes = (ctypes.c_void_p,)

        self._cf_data_get_byte_ptr = cf.CFDataGetBytePtr
        self._cf_data_get_byte_ptr.restype = ctypes.c_void_p
        self._cf_data_get_byte_ptr.argtypes = (ctypes.c_void_p,)

        self._cf_data_get_length = cf.CFDataGetLength
        self._cf_data_get_length.restype = ctypes.c_int32
        self._cf_data_get_length.argtypes = (ctypes.c_void_p,)

        self._cf_release = cf.CFRelease
        self._cf_release.restype = None
        self._cf_release.argtypes = (ctypes.c_void_p,)

        self._key_callbacks = cf.kCFTypeDictionaryKeyCallBacks
        self._val_callbacks = cf.kCFTypeDictionaryValueCallBacks

        @functools.singledispatch
        def create_cf(ob: Any) -> Any:
            return ob

        @create_cf.register(bool)
        @create_cf.register(int)
        def _cf_int(val: bool | int) -> Any:
            if val.bit_length() > 31:
                raise OverflowError(val)
            return self._cf_num_create(None, 0x9, ctypes.byref(ctypes.c_int32(val)))

        @create_cf.register(str)
        def _cf_str(val: str) -> Any:
            return self._cf_str_create(None, val.encode("utf8"), 0x08000100)

        self._create_cf = create_cf

    def _k(self, name: str) -> ctypes.c_void_p:
        try:
            return ctypes.c_void_p.in_dll(self._sec, name)
        except ValueError as exc:
            raise KeystoreError(f"Missing Security symbol {name!r}: {exc}") from exc

    def _create_query(self, **kwargs: Any) -> Any:
        return self._cf_dict_create(
            None,
            (ctypes.c_void_p * len(kwargs))(*map(self._k, kwargs.keys())),
            (ctypes.c_void_p * len(kwargs))(*map(self._create_cf, kwargs.values())),
            len(kwargs),
            self._key_callbacks,
            self._val_callbacks,
        )

    def get(self, name: str) -> str | None:
        q = self._create_query(
            kSecClass=self._k("kSecClassGenericPassword"),
            kSecMatchLimit=self._k("kSecMatchLimitOne"),
            kSecAttrService=SERVICE,
            kSecAttrAccount=name,
            kSecReturnData=True,
        )
        data = ctypes.c_void_p()
        try:
            status = int(self._sec_item_copy_matching(q, ctypes.byref(data)))
            if status in (-128, -25308):  # errSecUserCanceled, errSecInteractionNotAllowed
                raise KeystoreLocked(f"OSStatus {status}")
            if status == -25300:  # errSecItemNotFound
                return None
            if status != 0:
                raise KeystoreError(f"OSStatus {status}")
            try:
                return ctypes.string_at(
                    self._cf_data_get_byte_ptr(data),
                    self._cf_data_get_length(data),
                ).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise KeystoreError(f"Invalid UTF-8 in secret {name!r}: {exc}") from exc
        finally:
            if data:
                self._cf_release(data)
            if q:
                self._cf_release(q)

    def set(self, name: str, value: str) -> None:
        # SecItemAdd fails with errSecDuplicateItem (-25299) if the item already exists.
        self.delete(name)
        q = self._create_query(
            kSecClass=self._k("kSecClassGenericPassword"),
            kSecAttrService=SERVICE,
            kSecAttrAccount=name,
            kSecValueData=value,
        )
        try:
            status = int(self._sec_item_add(q, None))
            if status != 0:
                raise KeystoreError(f"OSStatus {status}")
        finally:
            if q:
                self._cf_release(q)

    def delete(self, name: str) -> None:
        q = self._create_query(
            kSecClass=self._k("kSecClassGenericPassword"),
            kSecAttrService=SERVICE,
            kSecAttrAccount=name,
        )
        try:
            status = int(self._sec_item_delete(q))
            if status not in (0, -25300):  # errSecItemNotFound
                raise KeystoreError(f"OSStatus {status}")
        finally:
            if q:
                self._cf_release(q)


def _detect() -> _KeystoreBackend | None:
    if os.environ.get("WRITERAGENT_KEYSTORE") == "0":
        return None
    try:
        if sys.platform == "win32":
            return _WinCredentials()
        if sys.platform == "darwin":
            return _MacKeychain()
        return _LibSecret()
    except (OSError, AttributeError, KeystoreError) as exc:
        log.info("OS keystore unavailable (%s); secrets stay in writeragent.json", exc)
        return None


def _get_backend() -> _KeystoreBackend | None:
    global _detected, _resolved
    if _detected:
        return _resolved
    with _init_lock:
        if not _detected:
            _resolved = _detect()
            _detected = True
        return _resolved


def reset_for_tests(backend: Any = _UNSET) -> None:
    """Reset cached keystore state for unit tests (optionally installing *backend*)."""
    global _detected, _resolved, _writes_failed
    if backend is _UNSET:
        _detected = False
        _resolved = None
    else:
        _detected = True
        _resolved = backend
    with _cache_lock:
        _cache.clear()
        _read_failures_logged.clear()
        _locked.clear()
    _writes_failed = False


def is_ref(value: object) -> bool:
    """True when *value* is a ``keystore:<token>`` reference string."""
    return isinstance(value, str) and value.startswith(REF_PREFIX)


def new_ref() -> str:
    """Return a fresh ``keystore:<8 hex>`` reference token.

    Each write generates a new token so ``config.save()`` sees a changed JSON
    value, writes the file, and emits ``config:changed``.
    """
    return REF_PREFIX + secrets.token_hex(4)


def backend_label() -> str:
    """Return the active OS keystore label, or ``""`` when no store is available."""
    backend = _get_backend()
    return backend.label if backend is not None else ""


def can_store() -> bool:
    """True when an OS keystore is available and no write has failed this session."""
    return _get_backend() is not None and not _writes_failed


def get_secret(name: str) -> str | None:
    """Read *name* from the OS keystore, returning ``None`` when absent or unreadable."""
    with _cache_lock:
        if name in _cache:
            return _cache[name]
        locked_at = _locked.get(name)
        if locked_at is not None and time.monotonic() - locked_at < LOCKED_RETRY_S:
            return None
    backend = _get_backend()
    if backend is None:
        return None
    try:
        value = backend.get(name)
    except KeystoreLocked as exc:
        # Not cached: a later read (Send, Settings) may prompt again and succeed.
        with _cache_lock:
            # A save or delete that landed while this read waited on the
            # unlock prompt wins; a name is never both cached and locked.
            if name in _cache:
                return _cache[name]
            if name not in _locked:
                log.warning("Secret %r unavailable: %s", name, exc)
            _locked[name] = time.monotonic()
        return None
    except KeystoreError as exc:
        with _cache_lock:
            if name not in _read_failures_logged:
                _read_failures_logged.add(name)
                log.warning("Failed to read secret %r from %s: %s", name, backend.label, exc)
            return _cache.setdefault(name, None)
    with _cache_lock:
        _locked.pop(name, None)
        return _cache.setdefault(name, value)


def is_locked(name: str) -> bool:
    """True when the last read of *name* found it in a locked keyring."""
    with _cache_lock:
        return name in _locked


def is_cached(name: str) -> bool:
    """True when *name* is in the in-memory cache (reads need no OS call)."""
    with _cache_lock:
        return name in _cache


def set_secret(name: str, value: str) -> bool:
    """Write *name* to the OS keystore; returns ``False`` on failure or missing store."""
    global _writes_failed
    backend = _get_backend()
    if backend is None or _writes_failed:
        return False
    try:
        backend.set(name, value)
    except KeystoreError as exc:
        _writes_failed = True
        log.warning("Failed to write secret %r to %s (%s); keeping plain text", name, backend.label, exc)
        return False
    with _cache_lock:
        _cache[name] = value
        _locked.pop(name, None)
    return True


def delete_secret(name: str) -> None:
    """Best-effort delete of *name* from the OS keystore."""
    backend = _get_backend()
    if backend is not None:
        try:
            backend.delete(name)
        except KeystoreError as exc:
            log.warning("Failed to delete secret %r from %s: %s", name, backend.label, exc)
    with _cache_lock:
        _cache[name] = None
        _locked.pop(name, None)
