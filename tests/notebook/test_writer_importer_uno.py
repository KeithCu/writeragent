# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live Writer smoke: Debug-menu Jupyter import + run on the small NumPy fixture.

Entry point is the same action the menubar uses
(``WriterAgent → Debug → Import Jupyter Notebook…``,
``org.extension.writeragent:scripting.import_ipynb``). The native runner cannot
click a modal FilePicker, so the test drives that picker to the fixture path
while still executing ``import_dialog._pick_ipynb_path`` / ``run_import_ipynb_dialog``.
It does **not** call ``import_ipynb_to_writer`` itself.

A sandbox ``Forbidden access to dunder attribute`` / ``__version__`` deny is a
hard failure (PR 453 treated that as a clean error). A worker
``ModuleNotFoundError`` for numpy is allowed when the venv has no NumPy.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

from plugin.testing_runner import native_test, show_window
from plugin.tests.testing_utils import with_native_doc

_SMALL_IPYNB = (
    Path(__file__).resolve().parents[1] / "fixtures" / "introduction-to-numpy-small.ipynb"
)
_IMPORT_ACTION = "scripting.import_ipynb"
_HEADINGS = (
    ("A Small Introduction to NumPy", 1),
    ("1. Creating Arrays", 2),
    ("2. Array Operations", 2),
)


class _DrivenFilePicker:
    """Stand-in for the UNO FilePicker: OK + fixture URL, no modal click."""

    def __init__(self, file_url: str) -> None:
        self._file_url = file_url
        self.calls: list[str] = []

    def initialize(self, _args: object) -> None:
        self.calls.append("initialize")

    def setTitle(self, _title: str) -> None:
        self.calls.append("setTitle")

    def appendFilter(self, _name: str, _glob: str) -> None:
        self.calls.append("appendFilter")

    def setCurrentFilter(self, _name: str) -> None:
        self.calls.append("setCurrentFilter")

    def execute(self) -> int:
        self.calls.append("execute")
        return 1

    def getFiles(self) -> tuple[str, ...]:
        self.calls.append("getFiles")
        return (self._file_url,)


def _drive_filepicker(fixture_path: Path):
    """Run the real ``_pick_ipynb_path`` against a FilePicker that returns *fixture_path*."""
    import uno

    import plugin.notebook.import_dialog as import_dialog

    orig = import_dialog._pick_ipynb_path
    file_url = uno.systemPathToFileUrl(str(fixture_path))
    picker = _DrivenFilePicker(file_url)

    def driven(ctx):
        class _Smgr:
            def createInstanceWithContext(self, service, c):
                if service == "com.sun.star.ui.dialogs.FilePicker":
                    return picker
                return ctx.getServiceManager().createInstanceWithContext(service, c)

        class _Ctx:
            def getServiceManager(self):
                return _Smgr()

        return orig(_Ctx())

    return patch.object(import_dialog, "_pick_ipynb_path", driven), picker


def _capture_msgbox(store: list):
    def _capture(ctx, title, message, *, box_type=1):
        store.append((str(title), str(message), box_type))

    return _capture


def _activate_doc(ctx, doc) -> None:
    """Menu handlers resolve the document via ``get_active_document``."""
    try:
        doc.getCurrentController().getFrame().activate()
    except Exception:
        pass
    try:
        from plugin.framework.uno_context import process_events_to_idle

        process_events_to_idle(ctx)
    except Exception:
        pass


def _paragraphs(doc) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        try:
            if hasattr(el, "supportsService") and not el.supportsService("com.sun.star.text.Paragraph"):
                continue
            style = str(el.getPropertyValue("ParaStyleName") or "")
            text = str(el.getString() or "")
        except Exception:
            continue
        out.append((style, text))
    return out


def _style_is_heading(style: str, level: int) -> bool:
    compact = (style or "").lower().replace(" ", "")
    return compact == f"heading{level}"


def _draw_control_names(doc) -> list[str]:
    names: list[str] = []
    dp = doc.getDrawPage()
    for i in range(dp.getCount()):
        shape = dp.getByIndex(i)
        try:
            if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
                continue
            name = str(getattr(shape.Control, "Name", "") or "")
        except Exception:
            continue
        if name:
            names.append(name)
    return names


def _output_text_for_cell(doc, cell) -> str:
    from plugin.notebook.notebook_runner import (
        _cursor_after_bookmark,
        _is_next_cell_boundary,
        _paragraph_string,
    )
    from plugin.notebook.writer_importer import _STYLE_NOTEBOOK_IN, _resolve_para_style

    start = _cursor_after_bookmark(doc, cell.output_start_bookmark)
    if start is None:
        return ""
    text = doc.getText()
    notebook_in = _resolve_para_style(doc, _STYLE_NOTEBOOK_IN)
    end = text.createTextCursorByRange(start)
    while end.gotoNextParagraph(False):
        if _is_next_cell_boundary(end.ParaStyleName, _paragraph_string(end), notebook_in):
            end.gotoStartOfParagraph(False)
            break
    else:
        end.gotoEnd(False)
    sel = text.createTextCursorByRange(start)
    sel.gotoRange(end.getStart(), True)
    return str(sel.getString() or "")


def _tail_text(doc, n_chars: int = 400) -> str:
    return (doc.getText().getString() or "")[-n_chars:]


def _run_blob(result, output: str) -> str:
    return "\n".join(part for part in (result.status, result.message, output) if part)


def _is_dunder_version_forbid(blob: str) -> bool:
    """True when the sandbox still denies ``np.__version__`` (PR 453 hid this as a clean error)."""
    text = blob or ""
    if "Forbidden access to dunder attribute" in text:
        return True
    # InterpreterError text is ``Forbidden access to dunder attribute: __version__``.
    return "Forbidden" in text and "__version__" in text


def _is_missing_numpy(blob: str) -> bool:
    """Worker venv has no NumPy — environment issue, not a dunder-jail regression."""
    text = blob or ""
    if "numpy" not in text.lower():
        return False
    return (
        "ModuleNotFoundError" in text
        or "No module named" in text
        or "ImportError" in text
    )


@native_test
def test_debug_menu_import_ipynb_action_registered(ctx):
    from plugin.framework.main_shared import get_action_handler

    handler = get_action_handler(_IMPORT_ACTION)
    assert handler is not None, (
        "Debug menu action scripting.import_ipynb is not registered "
        "(WriterAgent → Debug → Import Jupyter Notebook…)"
    )


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_debug_menu_import_and_run_small_numpy_notebook(ctx, doc):
    assert _SMALL_IPYNB.is_file(), f"missing fixture {_SMALL_IPYNB}"

    nb_log = logging.getLogger("writeragent.notebook")
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    nb_log.addHandler(stream)
    old_level = nb_log.level
    nb_log.setLevel(logging.INFO)

    try:
        _debug_menu_import_and_run(ctx, doc)
    finally:
        nb_log.removeHandler(stream)
        nb_log.setLevel(old_level)


def _debug_menu_import_and_run(ctx, doc) -> None:
    from plugin.framework.main_shared import get_action_handler
    from plugin.notebook.cell_registry import cell_id_to_hex, load_registry
    from plugin.notebook.form_lookup import index_form_control_models
    from plugin.notebook.notebook_controls import (
        ensure_form_design_mode_off,
        wire_all_notebook_run_buttons,
    )
    from plugin.notebook.notebook_runner import read_code_from_field, run_cell, run_cell_for_doc_hex
    from plugin.framework.uno_context import get_active_document

    handler = get_action_handler(_IMPORT_ACTION)
    assert handler is not None, "scripting.import_ipynb is not registered"

    _activate_doc(ctx, doc)

    active = get_active_document(ctx)
    assert active is not None, "no active document for Debug-menu import"
    try:
        assert active.RuntimeUID == doc.RuntimeUID, (
            "test Writer doc is not the current component; Debug import would hit another document"
        )
    except AttributeError:
        pass

    picker_patch, picker = _drive_filepicker(_SMALL_IPYNB)
    boxes = []
    capture = _capture_msgbox(boxes)

    with picker_patch, patch("plugin.notebook.import_dialog.msgbox", capture), patch(
        "plugin.notebook.notebook_runner.msgbox", capture
    ):
        # Same callable DispatchHandler runs for Addons.xcu M16f.
        handler()

    assert "initialize" in picker.calls and "execute" in picker.calls and "getFiles" in picker.calls, (
        f"FilePicker was not driven through _pick_ipynb_path: {picker.calls}"
    )

    completion = "\n".join(msg for _title, msg, _bt in boxes)
    assert "Imported notebook" in completion or "Cells: 6" in completion, (
        f"completion msgbox missing import stats: {boxes!r}"
    )
    assert "Cells: 6" in completion
    assert "code: 3" in completion
    assert "markdown: 3" in completion
    assert "Code input fields in document: 6" in completion

    body = doc.getText().getString() or ""
    assert body.strip(), "import did not write into the active Writer document"

    paras = _paragraphs(doc)
    para_text = [t for _s, t in paras]
    joined = "\n".join(para_text)
    for title, level in _HEADINGS:
        assert title in joined, f"heading {title!r} missing from body"
        assert f"# {title}" not in joined and f"## {title}" not in joined, (
            f"ATX hashes still visible for {title!r}"
        )
        matching = [(s, t) for s, t in paras if title in t]
        assert matching, f"no paragraph contains {title!r}"
        text = matching[0][1]
        assert not text.lstrip().startswith("#"), f"leading # on {title!r}: {text!r}"
        heading_hit = next((s for s, t in matching if _style_is_heading(s, level)), None)
        if heading_hit is None:
            families = doc.getStyleFamilies().getByName("ParagraphStyles")
            want = f"Heading {level}"
            if families.hasByName(want):
                raise AssertionError(f"{title!r} expected {want}, got {matching!r}")

    # Inline ``ndarray`` → HTML <code> when the filter works; do not fail the smoke on CharStyle.
    if "`ndarray`" in body:
        print("NOTE: inline backticks remain on ndarray (HTML filter flaky)", flush=True)
    else:
        assert "ndarray" in body

    state = load_registry(doc)
    assert state is not None, "notebook registry missing after Debug-menu import"
    assert len(state.code_cells) == 3
    field_names = [c.code_field_name for c in state.code_cells]
    assert field_names == ["nb_cell_1_code", "nb_cell_3_code", "nb_cell_5_code"]

    draw_names = _draw_control_names(doc)
    for field in field_names:
        assert field in draw_names, f"{field} missing from draw page: {draw_names}"
    for cell in state.code_cells:
        run_name = f"nb_run_{cell_id_to_hex(cell.cell_id)}"
        assert run_name in draw_names, f"{run_name} missing from draw page: {draw_names}"

    models = index_form_control_models(doc)
    for field in field_names:
        assert field in models, f"form lookup missed {field}"

    src1 = read_code_from_field(doc, "nb_cell_1_code")
    src3 = read_code_from_field(doc, "nb_cell_3_code")
    src5 = read_code_from_field(doc, "nb_cell_5_code")
    assert "import numpy" in src1
    assert "np.array" in src3
    assert "a1 * 2" in src5

    ensure_form_design_mode_off(doc)
    wired = wire_all_notebook_run_buttons(ctx, doc)
    assert wired == 3, f"expected wired 3/3 run buttons, got {wired}"

    # Shared notebook: kernel — run the three code cells in document order.
    results = []
    for cell in state.code_cells:
        results.append(run_cell(ctx, doc, cell.cell_id))
        out = _output_text_for_cell(doc, cell)
        result = results[-1]
        print(
            f"notebook run cell index={cell.index} field={cell.code_field_name} "
            f"status={result.status} message={result.message!r} output={out!r}",
            flush=True,
        )
        assert out.strip() or result.status == "error", (
            f"cell {cell.index} produced no output under its bookmark "
            f"status={result.status!r} message={result.message!r} "
            f"bookmarks={list(doc.getBookmarks().getElementNames())}"
        )

    state = load_registry(doc)
    assert state is not None
    out1 = _output_text_for_cell(doc, state.code_cells[0])
    out3 = _output_text_for_cell(doc, state.code_cells[1])
    out5 = _output_text_for_cell(doc, state.code_cells[2])
    outputs = (out1, out3, out5)
    tail = _tail_text(doc)

    # PR 453 treated a sandbox dunder deny as a clean error. That must fail this job.
    numpy_missing = False
    for cell, result, out in zip(state.code_cells, results, outputs):
        blob = _run_blob(result, out)
        assert not _is_dunder_version_forbid(blob), (
            f"cell {cell.index} still denied __version__ (must not be the outcome): "
            f"status={result.status!r} message={result.message!r} output={out!r}"
        )
        if result.status == "error" and "__version__" in (result.message or ""):
            raise AssertionError(
                f"cell {cell.index} error message still mentions __version__: {result.message!r}"
            )
        if result.status == "error" and _is_missing_numpy(blob):
            numpy_missing = True
            print(
                f"NOTE: worker venv has no numpy (environment issue, not a dunder deny): "
                f"cell {cell.index} {result.message}",
                flush=True,
            )
            continue
        if numpy_missing:
            print(
                f"NOTE: notebook run cell {cell.index} skipped strict ok "
                f"(numpy missing earlier): {result.message}",
                flush=True,
            )
            continue
        assert result.status == "ok", (
            f"cell {cell.index} expected ok (numpy is present); "
            f"status={result.status!r} message={result.message!r} output={out!r}"
        )

    if not numpy_missing:
        assert "NumPy Version" in out1 or any(ch.isdigit() for ch in out1), (
            f"cell 1 stdout missing version: {out1!r}"
        )
        assert "10" in out3 and "20" in out3 and "30" in out3, f"cell 3 array missing: {out3!r}"
        assert "20" in out5 and "40" in out5 and "60" in out5, f"cell 5 multiplied values missing: {out5!r}"
        later = doc.getText().getString() or ""
        ver_at = later.find("NumPy Version")
        arr_at = later.find("1. Creating Arrays")
        if ver_at >= 0 and arr_at >= 0:
            assert ver_at < arr_at, "cell 1 output was inserted after later markdown (document end dump)"
        assert "NumPy Version" not in tail or "Multiplied" in tail, (
            f"cell 1 output looks dumped at document end: {tail!r}"
        )
        mashed = [t for _s, t in _paragraphs(doc) if "NumPy Version" in t and "Cell 3: Markdown" in t]
        assert not mashed, f"stdout concatenated onto next heading: {mashed!r}"

    draw_after_run = _draw_control_names(doc)
    for field in field_names:
        assert field in draw_after_run, f"{field} vanished from draw page after run: {draw_after_run}"
    for cell in state.code_cells:
        run_name = f"nb_run_{cell_id_to_hex(cell.cell_id)}"
        assert run_name in draw_after_run, f"{run_name} vanished from draw page after run: {draw_after_run}"

    # Re-run cell 1 via the button/protocol path; output must replace, not append.
    cell0 = state.code_cells[0]
    before = _output_text_for_cell(doc, cell0)
    with patch("plugin.notebook.notebook_runner.msgbox", capture):
        run_cell_for_doc_hex(ctx, doc, cell_id_to_hex(cell0.cell_id))
    after = _output_text_for_cell(doc, cell0)
    needle = "NumPy Version"
    if needle in before:
        assert after.count(needle) == 1, f"re-run appended duplicate output: {after!r}"
    elif before.strip() and after.strip():
        snippet = before.strip()[:40]
        assert after.count(snippet) <= 1 or after == before or len(after) < len(before) * 2, (
            f"re-run looks like append: before={before!r} after={after!r}"
        )

    # clear_cell_output paragraph-expand: markdown between code cells must survive re-run.
    body_after = doc.getText().getString() or ""
    assert "A Small Introduction to NumPy" in body_after
    assert "1. Creating Arrays" in body_after
    assert "2. Array Operations" in body_after
    draw_after_rerun = _draw_control_names(doc)
    for field in field_names:
        assert field in draw_after_rerun, f"{field} vanished after re-run: {draw_after_rerun}"
    for cell in state.code_cells:
        run_name = f"nb_run_{cell_id_to_hex(cell.cell_id)}"
        assert run_name in draw_after_rerun, f"{run_name} vanished after re-run: {draw_after_rerun}"
    mashed_after = [t for _s, t in _paragraphs(doc) if "NumPy Version" in t and "Cell 3: Markdown" in t]
    assert not mashed_after, f"re-run mashed stdout onto next heading: {mashed_after!r}"

    import plugin.scripting.session_manager as sm

    with patch.object(sm, "_msgbox", lambda *args, **kwargs: None):
        sm.reset_workbook_python_session(ctx, doc)
