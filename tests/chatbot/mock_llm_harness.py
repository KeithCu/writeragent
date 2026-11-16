# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-process mock-LLM helpers for native sidebar tests (not shipped)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

try:
    from scripts.mock_llm_server import (
        MOCK_MODEL_ID,
        CompletionRule,
        MockLLMConfig,
        make_handler_class,
    )
except ImportError:
    MOCK_MODEL_ID = "mock-model"
    CompletionRule = Any  # type: ignore[assignment,misc]
    MockLLMConfig = Any  # type: ignore[assignment,misc]
    make_handler_class = None  # type: ignore[assignment,misc]


def mock_config(config: Any, **flags: Any) -> Any:
    """Mutate a ``MockLLMConfig`` (or similar) in place and return it."""
    for key, value in flags.items():
        setattr(config, key, value)
    return config


_CONFIG_BACKUP_SUFFIX = ".mock-session"


def _config_backup_path() -> str:
    from plugin.framework.config import get_config_file_path

    return get_config_file_path() + _CONFIG_BACKUP_SUFFIX


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _save_config_backup() -> None:
    """Snapshot the exact writeragent.json text before a mock session edits it.

    Storing the raw file text lets stop restore every key the session or live
    sidebar wrote, including model LRUs, support maps, and flags.
    """
    from plugin.framework.config import atomic_write_text, get_config_file_path

    config_path = get_config_file_path()
    backup_path = _config_backup_path()
    os.makedirs(os.path.dirname(backup_path) or ".", exist_ok=True)
    atomic_write_text(backup_path, json.dumps({"config": _read_text(config_path)}), fsync=True)


def _still_pointed_at_mock(text: str | None) -> bool:
    """Return True if *text* still points at an ephemeral 127.0.0.1 mock session.

    Used during crash recovery so a stale backup does not overwrite settings
    the user intentionally changed after a killed run.
    """
    from plugin.framework.config import parse_config_json_text

    if text is None:
        return False
    data = parse_config_json_text(text)
    if data is None:
        return False
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or data.get("text_model") != MOCK_MODEL_ID:
        return False
    try:
        host = urlsplit(endpoint).hostname
    except ValueError:
        return False
    return host == "127.0.0.1"


def _restore_config_backup(*, only_if_still_mock: bool) -> bool:
    """Restore writeragent.json from the sidecar backup and remove the sidecar.

    When *only_if_still_mock* is True (recovery at next start after a killed
    run), the backup is restored only if the live file still points at the mock;
    otherwise the stale backup is dropped. Clears the config cache under
    ``_config_write_lock`` because ``_load`` trusts its cache for 2 s.
    """
    import plugin.framework.config as fw_config
    from plugin.framework.config import atomic_write_text, get_config_file_path

    backup_path = _config_backup_path()
    backup_text = _read_text(backup_path)
    if backup_text is None:
        return False
    try:
        payload = json.loads(backup_text)
        if not isinstance(payload, dict) or "config" not in payload:
            raise ValueError("missing config key")
        saved_text = payload["config"]
        if saved_text is not None and not isinstance(saved_text, str):
            raise ValueError("invalid config value")
    except (ValueError, KeyError) as exc:
        print(
            f"mock_llm_harness: dropping malformed backup {backup_path} ({exc})",
            file=sys.stderr,
        )
        try:
            os.remove(backup_path)
        except FileNotFoundError:
            pass
        return False
    config_path = get_config_file_path()
    restored = False
    if not only_if_still_mock or _still_pointed_at_mock(_read_text(config_path)):
        with fw_config._config_write_lock:
            if saved_text is None:
                try:
                    os.remove(config_path)
                except FileNotFoundError:
                    pass
            else:
                atomic_write_text(config_path, saved_text, prefix=".writeragent-", fsync=True)
            fw_config._invalidate_config_cache()
        restored = True
    else:
        print(
            f"mock_llm_harness: dropping stale backup {backup_path} (config no longer points at mock)",
            file=sys.stderr,
        )
    try:
        os.remove(backup_path)
    except FileNotFoundError:
        pass
    return restored


class MockSidebarSession:
    """Running mock LLM handle for a live sidebar session."""

    def __init__(
        self,
        httpd: ThreadingHTTPServer,
        thread: threading.Thread,
        base_url: str,
        config: MockLLMConfig,
    ) -> None:
        self.httpd = httpd
        self.thread = thread
        self.base_url = base_url
        # Mutate in place for F5/F6/F16 (fail / delay) without restarting soffice.
        self.config = config


def start_mock_sidebar_session(
    *,
    delay_ms: int = 20,
    offline: bool = True,
    extra_config: dict[str, Any] | None = None,
    **flags: Any,
) -> MockSidebarSession:
    """Bind an ephemeral mock and point config at ``writeragent-mock``."""
    from plugin.framework.client.model_fetcher import set_text_model
    from plugin.framework.config import save

    if make_handler_class is None or MockLLMConfig is Any:
        raise RuntimeError("scripts.mock_llm_server is not available in stripped release builds")

    # Finish a killed run's cleanup before snapshotting, or we would snapshot mock state.
    _restore_config_backup(only_if_still_mock=True)
    _save_config_backup()

    httpd: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    try:
        config = MockLLMConfig(delay_ms=delay_ms, offline=offline)
        mock_config(config, **flags)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler_class(config))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address[:2]
        base_url = "http://%s:%s" % (host, port)

        updates: dict[str, Any] = {"endpoint": base_url, "audio.stt_provider": "endpoint"}
        if extra_config:
            updates.update(extra_config)
        save(updates)
        set_text_model(MOCK_MODEL_ID, update_lru=False)
        # Live sidebar is the OXT process. get_config caches mtime checks for 2s.
        if os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1":
            time.sleep(2.1)
    except BaseException:
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if thread is not None:
            try:
                thread.join(timeout=2)
            except Exception:
                pass
        _restore_config_backup(only_if_still_mock=False)
        raise

    return MockSidebarSession(httpd, thread, base_url, config)


def stop_mock_sidebar_session(session: MockSidebarSession | None) -> None:
    if session is None:
        return
    try:
        session.httpd.shutdown()
        session.httpd.server_close()
        session.thread.join(timeout=2)
    except Exception:
        pass
    _restore_config_backup(only_if_still_mock=False)


def require_send_listener(*, skip_if_missing: bool = True, frame: Any = None, doc: Any = None):
    """Live ``SendButtonListener``. Skip or fail if the chat deck is not wired.

    Pass *frame* or *doc* for the dual-deck Packet (Writer vs Calc).
    """
    from plugin.chatbot.sidebar_test_hooks import send_listener, send_listener_for_doc

    sl = send_listener_for_doc(doc) if doc is not None else send_listener(frame)
    if sl is None:
        msg = "WriterAgent chat sidebar not wired (make test-mock-sidebar uses your LO profile)"
        if skip_if_missing:
            raise unittest.SkipTest(msg)
        raise AssertionError(msg)
    return sl


def write_budget_csv(path: str) -> None:
    """Tiny Amount column so Packet P can write a Total row."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("Item,Amount\nApples,10\nBananas,20\n")


def write_minimal_ods(path: str) -> None:
    """Valid empty-ish ODS (Item/Amount) for file-URL Calc open — not factory/scalc."""
    import zipfile

    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">'
        '<manifest:file-entry manifest:full-path="/" manifest:version="1.2" '
        'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
        "<office:body><office:spreadsheet>"
        '<table:table table:name="Sheet1">'
        "<table:table-row>"
        '<table:table-cell office:value-type="string"><text:p>Item</text:p></table:table-cell>'
        '<table:table-cell office:value-type="string"><text:p>Amount</text:p></table:table-cell>'
        "</table:table-row>"
        "<table:table-row>"
        '<table:table-cell office:value-type="string"><text:p>Apples</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="10">'
        "<text:p>10</text:p></table:table-cell>"
        "</table:table-row>"
        "<table:table-row>"
        '<table:table-cell office:value-type="string"><text:p>Bananas</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="20">'
        "<text:p>20</text:p></table:table-cell>"
        "</table:table-row>"
        "</table:table></office:spreadsheet></office:body></office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/manifest.xml", manifest)
        zf.writestr("content.xml", content)


def _iter_office_docs(ctx: Any) -> list[Any]:
    from plugin.chatbot.sidebar_test_hooks import desktop_from_ctx

    docs: list[Any] = []
    try:
        comps = desktop_from_ctx(ctx).getComponents()
        enum = comps.createEnumeration() if comps is not None else None
    except Exception:
        return docs
    while enum is not None:
        try:
            more = enum.hasMoreElements()
        except Exception:
            break
        if more is not True and more != 1:
            break
        try:
            elem = enum.nextElement()
        except Exception:
            break
        try:
            if elem is not None and elem.supportsService("com.sun.star.document.OfficeDocument"):
                docs.append(elem)
        except Exception:
            continue
    return docs


def find_open_calc(ctx: Any) -> Any:
    from plugin.framework.doc_type import is_calc

    for doc in _iter_office_docs(ctx):
        try:
            if is_calc(doc):
                return doc
        except Exception:
            continue
    return None


def find_open_writer(ctx: Any) -> Any:
    from plugin.framework.doc_type import is_writer

    for doc in _iter_office_docs(ctx):
        try:
            if is_writer(doc):
                return doc
        except Exception:
            continue
    return None


def open_calc_for_dual_sidebar(ctx: Any, *, timeout: float = 15.0) -> Any:
    """Open Calc for Packet P via the shared E12 helper (keep Writer; VCL factory).

    Do not ``loadComponentFromURL('private:factory/scalc')`` from the URP client
    after a WriterAgent deck is visible. Show the Writer deck first so
    ``OPEN_CALC`` can post onto that listener's ``MainThread``.
    """
    from plugin.chatbot.sidebar_test_hooks import open_calc_document

    try:
        return open_calc_document(ctx, timeout=timeout)
    except Exception:
        return None


def seed_budget_sheet(calc: Any) -> None:
    """Guarantee Item/Amount rows on a factory Calc (E12 open is empty)."""
    if calc is None:
        return
    try:
        sheet = calc.getSheets().getByIndex(0)
        if not (sheet.getCellByPosition(0, 0).getString() or "").strip():
            sheet.getCellByPosition(0, 0).setString("Item")
            sheet.getCellByPosition(1, 0).setString("Amount")
            sheet.getCellByPosition(0, 1).setString("Apples")
            sheet.getCellByPosition(1, 1).setValue(10)
            sheet.getCellByPosition(0, 2).setString("Bananas")
            sheet.getCellByPosition(1, 2).setValue(20)
    except Exception:
        pass


def calc_total_formula(calc: Any) -> str:
    if calc is None:
        return ""
    try:
        sheet = calc.getSheets().getByIndex(0)
        return str(sheet.getCellByPosition(1, 3).getFormula() or sheet.getCellByPosition(1, 3).getString() or "")
    except Exception:
        return ""


def finish_immediately_after_peer_sends(captures: list[dict[str, Any]]) -> bool:
    """True when each peer-send decision is followed by a finish tool.

    Locks #673: waiting after accepted deadlocks the peer.
    """
    decided_rows = [row.get("decided_tools") or [] for row in captures]
    saw_send = False
    finished_after = 0
    sends = 0
    for tools in decided_rows:
        if "peer_send_work" in tools or "peer_send_result" in tools:
            saw_send = True
            sends += 1
            continue
        if saw_send and (
            "workflow_finished" in tools or "final_answer" in tools
        ):
            finished_after += 1
            saw_send = False
    return sends > 0 and finished_after >= sends


__all__ = (
    "CompletionRule",
    "MockSidebarSession",
    "calc_total_formula",
    "find_open_calc",
    "find_open_writer",
    "finish_immediately_after_peer_sends",
    "mock_config",
    "open_calc_for_dual_sidebar",
    "require_send_listener",
    "seed_budget_sheet",
    "start_mock_sidebar_session",
    "stop_mock_sidebar_session",
    "write_budget_csv",
    "write_minimal_ods",
)
