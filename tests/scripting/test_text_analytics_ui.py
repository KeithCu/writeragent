# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for TextAnalyticsDialog (modeless UI)."""

from __future__ import annotations

from typing import Any


from plugin.scripting.text_analytics_ui import TextAnalyticsDialog


class FakeModel:
    def __init__(self, text: str = "") -> None:
        self.Text = text
        self.Enabled = True


class FakeControl:
    def __init__(self, text: str = "", state: int = 1) -> None:
        self._model = FakeModel(text)
        self.State = state
        self.enabled = True
        self.listeners: list[Any] = []

    def getModel(self) -> FakeModel:
        return self._model

    def setEnable(self, val: bool) -> None:
        self.enabled = val
        self._model.Enabled = val

    def addActionListener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def click(self) -> None:
        for listener in self.listeners:
            listener.actionPerformed(None)


class FakeDialog:
    def __init__(self) -> None:
        self.controls: dict[str, FakeControl] = {
            "ResultsEdit": FakeControl(),
            "ChkScope": FakeControl(state=1),
            "BtnRead": FakeControl(),
            "BtnEntities": FakeControl(),
            "BtnPhrases": FakeControl(),
            "BtnTopics": FakeControl(),
            "BtnSentiment": FakeControl(),
            "BtnCheck": FakeControl(),
            "BtnInsert": FakeControl(),
            "BtnClose": FakeControl(),
        }

    def getControl(self, cid: str) -> FakeControl | None:
        return self.controls.get(cid)


def _make_dialog() -> tuple[TextAnalyticsDialog, FakeDialog]:
    dlg_obj = TextAnalyticsDialog.__new__(TextAnalyticsDialog)
    dlg_obj._ctx = object()
    fake_dlg = FakeDialog()
    dlg_obj._dlg = fake_dlg
    dlg_obj._closed = False
    dlg_obj._busy = False
    dlg_obj._last_result = None
    dlg_obj._top_listener = None
    return dlg_obj, fake_dlg


def test_compute_schedules_dedicated_worker_and_applies_on_main_thread(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs",
        lambda doc, helper, scope="whole": ("A" * 30, {}),
    )

    recorded_worker: list[tuple[Any, str, bool]] = []

    def fake_run_in_background(func, name="", dedicated=False):
        recorded_worker.append((func, name, dedicated))

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_in_background", fake_run_in_background)

    def failing_run_rpc(*args, **kwargs):
        raise AssertionError("run_text_analytics should not be called synchronously")

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_text_analytics", failing_run_rpc)

    dlg_obj._compute(fake_dlg, "readability", "whole")

    res_ctrl = fake_dlg.controls["ResultsEdit"]
    assert res_ctrl.getModel().Text == "Analyzing..."
    assert dlg_obj._busy is True
    assert fake_dlg.controls["BtnRead"].enabled is False
    assert fake_dlg.controls["BtnInsert"].enabled is False
    assert fake_dlg.controls["BtnClose"].enabled is True
    assert len(recorded_worker) == 1
    worker_fn, name, dedicated = recorded_worker[0]
    assert name == "text-analytics-dialog"
    assert dedicated is True

    # Now run worker with immediate post_to_main_thread and mock RPC return
    readability_result = {
        "status": "ok",
        "result": {
            "readability": {"flesch_reading_ease": 65.5},
        },
    }
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_text_analytics", lambda *a, **kw: readability_result)
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.post_to_main_thread", lambda fn: fn())

    worker_fn()

    assert dlg_obj._busy is False
    assert fake_dlg.controls["BtnRead"].enabled is True
    assert fake_dlg.controls["BtnInsert"].enabled is True
    assert dlg_obj._last_result == readability_result
    assert "Readability (via textdescriptives):" in res_ctrl.getModel().Text


def test_topics_error_does_not_become_last_result(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs",
        lambda doc, helper, scope="whole": ("A" * 30, {}),
    )

    recorded_worker: list[Any] = []
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.run_in_background",
        lambda func, name="", dedicated=False: recorded_worker.append(func),
    )

    dlg_obj._compute(fake_dlg, "topics", "whole")
    assert len(recorded_worker) == 1
    worker_fn = recorded_worker[0]

    topics_err_envelope = {
        "status": "error",
        "code": "TEXT_ANALYTICS_ERROR",
        "helper": "topics",
        "message": "install scikit-learn for topics",
        "result": {
            "topics": [],
            "error": "MISSING_PACKAGE",
            "install_hint": "scikit-learn (part of the Data Analysis / EDA stack)",
            "install": "uv pip install scikit-learn",
        },
    }
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_text_analytics", lambda *a, **kw: topics_err_envelope)
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.post_to_main_thread", lambda fn: fn())

    worker_fn()

    assert dlg_obj._last_result is None
    res_text = fake_dlg.controls["ResultsEdit"].getModel().Text
    assert "Suggested install: uv pip install scikit-learn" in res_text


def test_worker_exception_uses_existing_analysis_msgid(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs",
        lambda doc, helper, scope="whole": ("A" * 30, {}),
    )

    recorded_worker: list[Any] = []
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.run_in_background",
        lambda func, name="", dedicated=False: recorded_worker.append(func),
    )

    dlg_obj._compute(fake_dlg, "readability", "whole")
    worker_fn = recorded_worker[0]

    def failing_rpc(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_text_analytics", failing_rpc)
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.post_to_main_thread", lambda fn: fn())

    worker_fn()

    assert dlg_obj._last_result is None
    res_text = fake_dlg.controls["ResultsEdit"].getModel().Text
    assert res_text.startswith("Error: boom")
    assert "Click Check Venv" in res_text


def test_diagnostics_exception_has_no_install_command(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: None)

    recorded_worker: list[Any] = []
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.run_in_background",
        lambda func, name="", dedicated=False: recorded_worker.append(func),
    )

    dlg_obj._compute(fake_dlg, "diagnostics", "whole")
    worker_fn = recorded_worker[0]

    def failing_rpc(*a, **kw):
        raise RuntimeError("no python")

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_text_analytics", failing_rpc)
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.post_to_main_thread", lambda fn: fn())

    worker_fn()

    res_text = fake_dlg.controls["ResultsEdit"].getModel().Text
    assert res_text == "Error from the Text Analytics worker:\nno python"
    assert "xx_sent_ud_sm" not in res_text
    assert "uv pip install" not in res_text


def test_failed_validation_clears_previous_result(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    dlg_obj._last_result = {"status": "ok", "result": {}}

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs",
        lambda doc, helper, scope="selection": ("", {}),
    )

    called_background = False

    def fake_run_in_background(*args, **kwargs):
        nonlocal called_background
        called_background = True

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_in_background", fake_run_in_background)

    dlg_obj._compute(fake_dlg, "readability", "selection")

    assert fake_dlg.controls["ResultsEdit"].getModel().Text == "Nothing is selected."
    assert dlg_obj._last_result is None
    assert called_background is False


def test_short_text_keeps_not_enough_text_message(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs",
        lambda doc, helper, scope="whole": ("short", {}),
    )

    dlg_obj._compute(fake_dlg, "readability", "whole")
    assert fake_dlg.controls["ResultsEdit"].getModel().Text == "Not enough text in the chosen scope."


def test_compute_disposed_document_sets_closed_message(monkeypatch):
    from plugin.framework.errors import DocumentDisposedError

    dlg_obj, fake_dlg = _make_dialog()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)

    def fake_resolve(*args, **kwargs):
        raise DocumentDisposedError("document closed")

    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs",
        fake_resolve,
    )

    called_background = False

    def fake_run_in_background(*args, **kwargs):
        nonlocal called_background
        called_background = True

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_in_background", fake_run_in_background)

    dlg_obj._compute(fake_dlg, "readability", "whole")
    assert fake_dlg.controls["ResultsEdit"].getModel().Text == "The document was closed."
    assert dlg_obj._last_result is None
    assert called_background is False



def test_busy_click_does_not_clear_or_reschedule(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    sentinel = {"status": "ok", "result": {"sentinel": True}}
    dlg_obj._busy = True
    dlg_obj._last_result = sentinel

    called_background = False

    def fake_run_in_background(*args, **kwargs):
        nonlocal called_background
        called_background = True

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_in_background", fake_run_in_background)

    dlg_obj._compute(fake_dlg, "readability", "whole")

    assert dlg_obj._last_result is sentinel
    assert called_background is False


def test_closed_dialog_drops_posted_result(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs",
        lambda doc, helper, scope="whole": ("A" * 30, {}),
    )

    recorded_worker: list[Any] = []
    monkeypatch.setattr(
        "plugin.scripting.text_analytics_ui.run_in_background",
        lambda func, name="", dedicated=False: recorded_worker.append(func),
    )

    dlg_obj._compute(fake_dlg, "readability", "whole")
    worker_fn = recorded_worker[0]

    readability_result = {
        "status": "ok",
        "result": {
            "readability": {"flesch_reading_ease": 65.5},
        },
    }
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_text_analytics", lambda *a, **kw: readability_result)

    def close_and_post(fn):
        dlg_obj._closed = True
        fn()

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.post_to_main_thread", close_and_post)

    worker_fn()

    assert fake_dlg.controls["ResultsEdit"].getModel().Text == "Analyzing..."


def test_insert_delegates_to_host_helper(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    fake_doc = object()
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: fake_doc)
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)

    def forbidden_insert(*args, **kwargs):
        raise AssertionError("insert_content_at_position should not be called by dialog")

    monkeypatch.setattr("plugin.writer.format.insert_content_at_position", forbidden_insert)

    recorded_insert: list[tuple[Any, Any, Any]] = []

    def fake_insert(ctx, doc, res):
        recorded_insert.append((ctx, doc, res))
        return 1

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.insert_text_analytics_result_into_doc", fake_insert)

    fake_result = {"status": "ok", "result": {"readability": {"score": 10}}}
    dlg_obj._last_result = fake_result

    dlg_obj._insert_report(fake_dlg)

    assert len(recorded_insert) == 1
    assert recorded_insert[0] == (dlg_obj._ctx, fake_doc, fake_result)
    assert fake_dlg.controls["ResultsEdit"].getModel().Text.endswith("Inserted after selection/caret.")


def test_topics_checkbox_off_passes_selection_scope(monkeypatch):
    dlg_obj, fake_dlg = _make_dialog()
    fake_dlg.controls["ChkScope"].State = 0
    dlg_obj._wire(fake_dlg)

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.get_active_document", lambda ctx: object())
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.is_writer", lambda doc: True)

    recorded_call: list[tuple[str, str]] = []

    def fake_resolve(doc, helper, scope="whole"):
        recorded_call.append((helper, scope))
        return "A" * 30, {}

    monkeypatch.setattr("plugin.scripting.text_analytics_ui.resolve_text_analytics_document_inputs", fake_resolve)
    monkeypatch.setattr("plugin.scripting.text_analytics_ui.run_in_background", lambda *a, **kw: None)

    fake_dlg.controls["BtnTopics"].click()

    assert len(recorded_call) == 1
    assert recorded_call[0] == ("topics", "selection")
