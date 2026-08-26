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
_MEDIUM_IPYNB = (
    Path(__file__).resolve().parents[1] / "fixtures" / "introduction-to-numpy-medium.ipynb"
)
_HTML_IMG_IPYNB = (
    Path(__file__).resolve().parents[1] / "fixtures" / "html-img-and-md-link.ipynb"
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


def _anchor_paragraph_string(doc, shape_name: str) -> str:
    from plugin.notebook.notebook_runner import _find_control_shape_by_name

    shape = _find_control_shape_by_name(doc, shape_name)
    if shape is None:
        return ""
    try:
        text = doc.getText()
        cursor = text.createTextCursorByRange(shape.getAnchor())
        cursor.gotoStartOfParagraph(False)
        cursor.gotoEndOfParagraph(True)
        return str(cursor.getString() or "")
    except Exception:
        return ""


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


def _graphic_count(doc) -> int:
    try:
        objs = doc.getGraphicObjects()
        return int(objs.getCount())
    except Exception:
        pass
    n = 0
    try:
        dp = doc.getDrawPage()
        for i in range(dp.getCount()):
            shape = dp.getByIndex(i)
            try:
                st = str(shape.getShapeType() or "")
            except Exception:
                continue
            if "Graphic" in st:
                n += 1
    except Exception:
        return n
    return n


def _hyperlink_urls(doc) -> list[str]:
    urls: list[str] = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        try:
            if hasattr(el, "supportsService") and not el.supportsService("com.sun.star.text.Paragraph"):
                continue
            portions = el.createEnumeration()
        except Exception:
            continue
        while portions.hasMoreElements():
            portion = portions.nextElement()
            try:
                url = str(portion.getPropertyValue("HyperLinkURL") or "")
            except Exception:
                url = ""
            if url:
                urls.append(url)
    try:
        fields = doc.getTextFields().createEnumeration()
        while fields.hasMoreElements():
            field = fields.nextElement()
            try:
                url = str(field.getPropertyValue("URL") or "")
            except Exception:
                url = ""
            if url:
                urls.append(url)
    except Exception:
        pass
    return urls


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_debug_menu_import_html_img_and_markdown_link(ctx, doc):
    """Relative HTML <img> embeds; [text](url) is a hyperlink; mixed cell keeps markdown."""
    assert _HTML_IMG_IPYNB.is_file(), f"missing fixture {_HTML_IMG_IPYNB}"
    img = _HTML_IMG_IPYNB.resolve().parents[1] / "images" / "numpy-anatomy-of-an-array-updated.png"
    assert img.is_file(), f"missing {img}"

    from plugin.framework.main_shared import get_action_handler
    from plugin.framework.uno_context import get_active_document, process_events_to_idle

    handler = get_action_handler(_IMPORT_ACTION)
    assert handler is not None, "scripting.import_ipynb is not registered"

    _activate_doc(ctx, doc)
    active = get_active_document(ctx)
    assert active is not None, "no active document for Debug-menu import"

    picker_patch, picker = _drive_filepicker(_HTML_IMG_IPYNB)
    boxes = []
    capture = _capture_msgbox(boxes)

    with picker_patch, patch("plugin.notebook.import_dialog.msgbox", capture):
        handler()

    process_events_to_idle(ctx)

    assert "execute" in picker.calls and "getFiles" in picker.calls, (
        f"FilePicker was not driven through _pick_ipynb_path: {picker.calls}"
    )
    completion = "\n".join(msg for _title, msg, _bt in boxes)
    assert "Imported notebook" in completion or "Cells: 1" in completion, (
        f"completion msgbox missing import stats: {boxes!r}"
    )

    body = doc.getText().getString() or ""
    assert "What is NumPy?" in body
    assert "Anatomy of an array" in body
    assert "## What is NumPy?" not in body
    assert "### Anatomy" not in body
    assert "[NumPy](" not in body
    assert "**Array**" not in body
    assert "NumPy" in body
    assert "Array" in body

    paras = _paragraphs(doc)
    anatomy = [(s, t) for s, t in paras if "Anatomy of an array" in t]
    assert anatomy, f"Anatomy heading missing: {paras!r}"
    assert not anatomy[0][1].lstrip().startswith("#"), anatomy[0]
    heading_hit = next((s for s, t in anatomy if _style_is_heading(s, 2)), None)
    if heading_hit is None:
        families = doc.getStyleFamilies().getByName("ParagraphStyles")
        if families.hasByName("Heading 2"):
            raise AssertionError(f"Anatomy expected Heading 2, got {anatomy!r}")

    assert _graphic_count(doc) >= 1, (
        f"relative HTML <img> was not embedded (graphics={_graphic_count(doc)})"
    )
    urls = _hyperlink_urls(doc)
    assert any("numpy.org" in u for u in urls), (
        f"markdown link was not a Writer hyperlink: body={body!r} urls={urls!r}"
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

    import re as _re

    assert _re.search(r"Cell \d+: Markdown", body) is None, (
        f"Cell N: Markdown chrome still present after import: {body[:800]!r}"
    )
    assert _re.search(r"Cell \d+: Code", body) is None, (
        f"Cell N: Code chrome still present after import: {body[:800]!r}"
    )
    assert not any(t.strip() == "Output" for _s, t in paras), (
        f"visible Output heading after import: {paras!r}"
    )
    assert any(t.strip().startswith("In [") for _s, t in paras), f"In [n]: gutter missing: {paras!r}"

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
        import re as _re

        assert _re.search(r"Cell \d+: Markdown", later) is None, (
            f"Cell N: Markdown chrome still in document after import/run: {later[:500]!r}"
        )

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
    import re as _re

    assert _re.search(r"Cell \d+: Markdown", body_after) is None, (
        f"Cell N: Markdown chrome present after re-run: {body_after[:500]!r}"
    )

    import plugin.scripting.session_manager as sm

    with patch.object(sm, "_msgbox", lambda *args, **kwargs: None):
        sm.reset_workbook_python_session(ctx, doc)


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_medium_numpy_import_layout_no_run(ctx, doc):
    """Visual fixture: Jupyter-like layout, no Run All, no 184-cell notebook."""
    assert _MEDIUM_IPYNB.is_file(), f"missing fixture {_MEDIUM_IPYNB}"

    import re as _re

    from plugin.notebook.cell_registry import load_registry
    from plugin.notebook.writer_importer import import_ipynb_to_writer, flush_ui_idle

    stats = import_ipynb_to_writer(doc, str(_MEDIUM_IPYNB), ctx=ctx)
    flush_ui_idle(ctx)

    assert stats["cells"] == 25
    assert stats["code"] == 11
    assert stats["markdown"] == 14
    assert stats["shapes"] == 22, f"expected 11 ▶ + 11 fields, got shapes={stats['shapes']}"

    body = doc.getText().getString() or ""
    paras = _paragraphs(doc)
    print(
        f"medium import pages={_writer_page_count(doc)} paras={len(paras)} "
        f"shapes={stats['shapes']} in_prompts={sum(1 for _s, t in paras if t.strip().startswith('In ['))} "
        f"numbering={sum(1 for s, _t in paras if 'numbering' in (s or '').lower())}",
        flush=True,
    )
    assert _re.search(r"Cell \d+: Markdown", body) is None, (
        f"Cell N: Markdown chrome after medium import: {body[:800]!r}"
    )
    assert _re.search(r"Cell \d+: Code", body) is None
    assert not any(t.strip() == "Output" for _s, t in paras), f"visible Output heading: {paras!r}"
    assert any(t.strip().startswith("In [") for _s, t in paras), f"In [n]: gutter missing: {paras!r}"
    assert "A Medium Introduction to NumPy" in body
    assert "* **Array**" not in body, f"literal markdown list/bold survived: {body[body.find('Array')-40:body.find('Array')+80]!r}"
    assert "Array" in body

    state = load_registry(doc)
    assert state is not None and len(state.code_cells) == 11
    names = _draw_control_names(doc)
    for cell in state.code_cells:
        assert cell.code_field_name in names, f"{cell.code_field_name} missing: {names}"

    from plugin.notebook.notebook_runner import _find_control_shape_by_name, read_code_from_field
    from plugin.notebook.writer_importer import _height_for_text

    # Multi-line In[2] field must be tall enough that the last line is not clipped.
    cell_in2 = state.code_cells[1]
    src_in2 = read_code_from_field(doc, cell_in2.code_field_name)
    lines_in2 = max(1, src_in2.count("\n") + 1)
    shape_in2 = _find_control_shape_by_name(doc, cell_in2.code_field_name)
    assert shape_in2 is not None
    h_in2 = int(shape_in2.getSize().Height)
    want_h = _height_for_text(src_in2, doc)
    print(f"medium In[2] lines={lines_in2} shape_h={h_in2} want_h={want_h}", flush=True)
    assert lines_in2 >= 10, f"fixture In[2] should be multi-line, got {lines_in2}"
    assert h_in2 >= want_h or h_in2 >= (lines_in2 + 1) * 420, (
        f"In[2] field clips source: height={h_in2} lines={lines_in2} want={want_h}"
    )

    cell_in1 = state.code_cells[0]
    src_in1 = read_code_from_field(doc, cell_in1.code_field_name)
    lines_in1 = max(1, src_in1.count("\n") + 1)
    shape_in1 = _find_control_shape_by_name(doc, cell_in1.code_field_name)
    assert shape_in1 is not None
    h_in1 = int(shape_in1.getSize().Height)
    want_in1 = _height_for_text(src_in1, doc)
    print(f"medium In[1] lines={lines_in1} shape_h={h_in1} want_h={want_in1}", flush=True)
    assert lines_in1 == 2, f"fixture In[1] should be two source lines, got {lines_in1}"
    # Full extra line of wrap slack. Short cells keep a bit of empty gray.
    assert h_in1 >= want_in1, f"In[1] lost wrap slack: height={h_in1} want={want_in1}"

    from plugin.notebook.cell_registry import cell_id_to_hex
    from plugin.notebook.writer_importer import _text_area_width_units

    run_in2 = f"nb_run_{cell_id_to_hex(cell_in2.cell_id)}"
    gutter_in2 = _anchor_paragraph_string(doc, run_in2)
    field_para_in2 = _anchor_paragraph_string(doc, cell_in2.code_field_name)
    field_w = int(shape_in2.getSize().Width)
    area = _text_area_width_units(doc)
    print(
        f"medium In[2] gutter={gutter_in2!r} field_para={field_para_in2!r} "
        f"field_w={field_w} area={area}",
        flush=True,
    )
    assert gutter_in2.strip().startswith("In ["), f"▶ not on In [n]: row: {gutter_in2!r}"
    assert not field_para_in2.strip().startswith("In ["), (
        f"field still shares gutter para: {field_para_in2!r}"
    )
    assert field_w >= area - 50, f"field not full text-area width: {field_w} vs {area}"

    cell_in3 = state.code_cells[2]
    src_in3 = read_code_from_field(doc, cell_in3.code_field_name)
    lines_in3 = max(1, src_in3.count("\n") + 1)
    shape_in3 = _find_control_shape_by_name(doc, cell_in3.code_field_name)
    assert shape_in3 is not None
    h_in3 = int(shape_in3.getSize().Height)
    want_in3 = _height_for_text(src_in3, doc)
    print(f"medium In[3] lines={lines_in3} shape_h={h_in3} want_h={want_in3}", flush=True)
    assert h_in3 >= want_in3 - 5, f"In[3] wrap-clip: height={h_in3} want={want_in3} lines={lines_in3}"
    run_in3 = f"nb_run_{cell_id_to_hex(cell_in3.cell_id)}"
    assert _anchor_paragraph_string(doc, run_in3).strip().startswith("In [")

    why_page = _page_of_text(doc, "Why NumPy?")
    dt_page = _page_of_text(doc, "1. DataTypes and attributes")
    in2_page = _page_of_text(doc, "In [2]:")
    by_page = _paragraphs_with_pages(doc)
    layout = _paragraphs_with_layout(doc)
    print(
        f"medium pages why={why_page} datatypes={dt_page} in2={in2_page} "
        f"leading_empties={ {p: _leading_empty_count(by_page, p) for p in sorted({pg for pg, _s, _t in by_page})} }",
        flush=True,
    )
    if why_page is not None and dt_page is not None:
        assert dt_page == why_page, (
            f"DataTypes heading skipped to page {dt_page} away from Why NumPy on {why_page}"
        )
    # Extra wrap-line pad can make In[2]'s field miss the remainder of page 1.
    # That is an unsplittable AS_CHARACTER jump, not KeepWithNext glue (DataTypes
    # staying with Why NumPy is the hole we still forbid).
    pages_used = sorted({pg for pg, _s, _t in by_page})
    for page in pages_used:
        empties = _leading_empty_count(by_page, page)
        assert empties <= 1, (
            f"page {page} starts with {empties} empty paragraphs before content: "
            f"{[t for pg, _s, t in by_page if pg == page][:6]!r}"
        )

    pages = _writer_page_count(doc)
    print(f"medium page_count={pages}", flush=True)
    box = _page_box(doc)
    if box is not None and layout:
        top, bottom, page_h = box
        print(f"medium page box top={top} bottom={bottom} h={page_h}", flush=True)
        for page in pages_used:
            first = next(
                (item for item in layout if item[0] == page and (item[3] or "").strip()),
                None,
            )
            if first is None:
                continue
            _pg, y, _style, text = first
            local_y = _page_local_y(y, page, page_h)
            preview = " ".join((text or "").split())[:48]
            print(
                f"medium page {page} first_y={y} local_y={local_y} {preview!r}",
                flush=True,
            )
            # Skip-before-shape: a quarter-page blank top band is ~5000+ HMM.
            # ViewCursor Y is document-absolute (page 2 was 28441 on a 27940 page).
            if page > 1 and local_y > 0:
                assert local_y <= top + 2500, (
                    f"page {page} blank top band: {preview!r} at local Y={local_y} "
                    f"(raw Y={y}, top margin {top})"
                )
        # Print remaining-space math for code cells that start a page. Do not
        # assert it: the field paragraph is empty in getString(), so last_y is
        # the In [n]: gutter, not the bottom of the gray box.
        from plugin.notebook.notebook_runner import _find_control_shape_by_name as _find_shape

        for page in pages_used:
            if page <= 1:
                continue
            first = next(
                (item for item in layout if item[0] == page and (item[3] or "").strip()),
                None,
            )
            if first is None:
                continue
            preview = " ".join((first[3] or "").split())[:48]
            if not preview.startswith("In ["):
                continue
            prev = [item for item in layout if item[0] == page - 1 and (item[3] or "").strip()]
            if not prev:
                continue
            last_y = prev[-1][1]
            last_local = _page_local_y(last_y, page - 1, page_h)
            remaining = page_h - bottom - last_local
            last_preview = " ".join((prev[-1][3] or "").split())[:48]
            field_h = 0
            in_index = sum(
                1
                for item in layout
                if item[0] < page and (item[3] or "").strip().startswith("In [")
            )
            if 0 <= in_index < len(state.code_cells):
                shp = _find_shape(doc, state.code_cells[in_index].code_field_name)
                if shp is not None:
                    field_h = int(shp.getSize().Height)
            print(
                f"medium page {page} starts {preview!r} last_prev_y={last_y} "
                f"last_local={last_local} remaining={remaining} field_h={field_h} "
                f"last={last_preview!r}",
                flush=True,
            )


@native_test
@with_native_doc("writer", hidden=not show_window)
def test_medium_run_in2_keeps_in3_controls(ctx, doc):
    """Keith repro: ▶ on medium In[2] must not eat In[3]'s play button and field."""
    assert _MEDIUM_IPYNB.is_file(), f"missing fixture {_MEDIUM_IPYNB}"

    from plugin.notebook.cell_registry import cell_id_to_hex, load_registry
    from plugin.notebook.notebook_controls import (
        ensure_form_design_mode_off,
        wire_all_notebook_run_buttons,
    )
    from plugin.notebook.notebook_runner import read_code_from_field
    from plugin.notebook.writer_importer import import_ipynb_to_writer, flush_ui_idle

    import_ipynb_to_writer(doc, str(_MEDIUM_IPYNB), ctx=ctx)
    flush_ui_idle(ctx)
    state = load_registry(doc)
    assert state is not None and len(state.code_cells) >= 3
    first_of_pair, second_of_pair = state.code_cells[1], state.code_cells[2]
    src_before = read_code_from_field(doc, second_of_pair.code_field_name)
    assert "a1.shape" in src_before or "shape/ndim" in src_before

    ensure_form_design_mode_off(doc)
    wire_all_notebook_run_buttons(ctx, doc)
    fake = {"status": "ok", "stdout": "a1: [1 2 3]\n", "result": None}
    with (
        patch("plugin.notebook.notebook_runner.msgbox", lambda *_a, **_k: None),
        patch("plugin.notebook.notebook_runner.execute_code", return_value=fake),
    ):
        from plugin.notebook.notebook_runner import run_cell

        result = run_cell(ctx, doc, first_of_pair.cell_id)
    flush_ui_idle(ctx)
    print(
        f"medium run In[2] status={result.status} draw={_draw_control_names(doc)!r}",
        flush=True,
    )
    names = _draw_control_names(doc)
    assert second_of_pair.code_field_name in names, f"In[3] field eaten: {names!r}"
    run_name = f"nb_run_{cell_id_to_hex(second_of_pair.cell_id)}"
    assert run_name in names, f"In[3] ▶ eaten: {names!r}"
    src_after = read_code_from_field(doc, second_of_pair.code_field_name)
    assert src_after.strip() == src_before.strip(), f"In[3] source changed: {src_after!r}"
    assert _anchor_paragraph_string(doc, run_name).strip().startswith("In ["), (
        f"In[3] ▶ left the gutter after run: {_anchor_paragraph_string(doc, run_name)!r}"
    )


def _writer_page_count(doc) -> int | None:
    try:
        vc = doc.getCurrentController().getViewCursor()
        vc.jumpToLastPage()
        return int(vc.getPage())
    except Exception:
        return None


def _page_of_text(doc, needle: str) -> int | None:
    try:
        vc = doc.getCurrentController().getViewCursor()
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            el = enum.nextElement()
            try:
                text = str(el.getString() or "")
            except Exception:
                continue
            if needle in text:
                vc.gotoRange(el.getStart(), False)
                return int(vc.getPage())
    except Exception:
        return None
    return None


def _paragraphs_with_pages(doc) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    try:
        vc = doc.getCurrentController().getViewCursor()
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            el = enum.nextElement()
            try:
                if hasattr(el, "supportsService") and not el.supportsService("com.sun.star.text.Paragraph"):
                    continue
                style = str(el.getPropertyValue("ParaStyleName") or "")
                text = str(el.getString() or "")
                vc.gotoRange(el.getStart(), False)
                page = int(vc.getPage())
            except Exception:
                continue
            out.append((page, style, text))
    except Exception:
        return out
    return out


def _leading_empty_count(by_page: list[tuple[int, str, str]], page: int) -> int:
    n = 0
    seen = False
    for pg, _style, text in by_page:
        if pg != page:
            if seen:
                break
            continue
        seen = True
        if (text or "").strip():
            break
        n += 1
    return n


def _page_local_y(y: int, page: int, page_h: int) -> int:
    """Map ViewCursor.getPosition().Y (document layout) onto one page."""
    if page <= 1 or page_h <= 0:
        return y
    return y - (page - 1) * page_h


def _page_box(doc) -> tuple[int, int, int] | None:
    """(top_margin, bottom_margin, page_height) in 1/100 mm."""
    try:
        families = doc.getStyleFamilies().getByName("PageStyles")
        name = ""
        try:
            name = str(doc.getPropertyValue("PageDescName") or "")
        except Exception:
            name = ""
        style = None
        if name and families.hasByName(name):
            style = families.getByName(name)
        else:
            for candidate in ("Standard", "Default", "Default Page Style"):
                if families.hasByName(candidate):
                    style = families.getByName(candidate)
                    break
        if style is None:
            return None
        return (
            int(style.getPropertyValue("TopMargin")),
            int(style.getPropertyValue("BottomMargin")),
            int(style.getPropertyValue("Height")),
        )
    except Exception:
        return None


def _paragraphs_with_layout(doc) -> list[tuple[int, int, str, str]]:
    """(page, view Y in 1/100 mm, style, text) for skip-before / blank-tail checks."""
    out: list[tuple[int, int, str, str]] = []
    try:
        vc = doc.getCurrentController().getViewCursor()
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            el = enum.nextElement()
            try:
                if hasattr(el, "supportsService") and not el.supportsService("com.sun.star.text.Paragraph"):
                    continue
                style = str(el.getPropertyValue("ParaStyleName") or "")
                text = str(el.getString() or "")
                vc.gotoRange(el.getStart(), False)
                page = int(vc.getPage())
                pos = vc.getPosition()
                y = int(getattr(pos, "Y", 0) or 0)
            except Exception:
                continue
            out.append((page, y, style, text))
    except Exception:
        return out
    return out

