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
"""
Form tools for Writer, Calc, and Draw/Impress (shared registration: ToolWriterFormBase ∪ ToolDrawFormBase).
Adapted from OnlyOfficeAI patterns. Original source: onlyofficeai/scripts/helpers/helpers.js (generateForm)

These tools address live LibreOffice ControlShapes (com.sun.star.form.component.*).
They do not fill PDF/AcroForm widgets. Draw "paper forms" (empty TextShapes next
to labels) use get_draw_tree + fill_draw_fields / shape_upsert instead.
"""

import logging
import re
from typing import Any
from com.sun.star.awt import Point, Size
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from ..specialized_base import ToolWriterFormBase, enum_param, index_error
from plugin.doc.doc_type import is_calc, is_draw
from plugin.doc.text_helpers import clone_text_range
from plugin.doc.visual_helpers import get_active_draw_page
from plugin.draw.tree import CONTROL_TYPE_MAP, coerce_control_state
from plugin.framework import thread_guard
from plugin.framework.errors import format_error_payload, ToolExecutionError, is_real_disposal, reraise_if_disposed
from plugin.framework.queue_executor import run_on_main
from plugin.framework.uno_context import uno_same

log = logging.getLogger("writeragent.writer.forms")

from plugin.framework.constants import ALL_DOCUMENT_SERVICES

# One registration per tool name; union services for Writer + Calc + Draw/Impress (see AGENTS.md shared tools).
_FORM_DOC_SERVICES = list(ALL_DOCUMENT_SERVICES)


def _get_readable_type(model: Any) -> str:
    """Maps a UNO model back to a human-friendly type string."""
    for type_str, service in CONTROL_TYPE_MAP.items():
        if model.supportsService(service):
            return type_str
    return "unknown"


# Local aliases keep call sites short.
_is_spreadsheet_doc = is_calc
_is_draw_doc = is_draw
_get_form_draw_page = get_active_draw_page


def _no_form_draw_page_payload() -> dict[str, Any]:
    return format_error_payload(ToolExecutionError("No draw page available for form operations."))


def _resolve_form_page(doc: Any, page: Any = None) -> Any:
    """Active draw page, or a Draw/Impress page index when *page* is set.

    Writer/Calc ignore *page* (Writer has one canvas; Calc uses the active sheet).
    """
    if page is not None and _is_draw_doc(doc):
        try:
            from plugin.draw.bridge import DrawBridge

            return DrawBridge(doc).get_pages().getByIndex(int(page))
        except Exception as exc:
            # Bugfix: a closed document raises DisposedException from DrawBridge page lookup,
            # and this except turned that into a missing page / not-found result.
            # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
            # Any other failure stays on the existing missing-page path.
            if is_real_disposal(exc):
                raise
            return None
    return _get_form_draw_page(doc)


def _control_value_fields(model: Any) -> dict[str, Any]:
    """Current value/state for list/edit so Draw widgets are visible without guessing."""
    info = {}
    if hasattr(model, "Label"):
        info["label"] = model.Label
    if hasattr(model, "Text"):
        info["text"] = model.Text
    if hasattr(model, "State"):
        info["state"] = int(model.State)
    if hasattr(model, "StringItemList"):
        info["items"] = list(model.StringItemList)
    if hasattr(model, "SelectedItems"):
        info["selected"] = list(model.SelectedItems)
    return info


def _find_control_shape(dp: Any, index: int | None = None, name: Any = None) -> tuple[Any, Any, dict[str, Any] | None]:
    """Resolve a ControlShape by draw-page index or control/shape Name.

    Index is the draw-page shape index from form_list_controls (not a
    control-only ordinal). Non-controls between widgets shift that index;
    Name is the stable address.
    """
    if index is not None:
        assert isinstance(index, int)
        err = index_error(index, dp.getCount(), "draw-page shape")
        if err:
            return None, None, format_error_payload(ToolExecutionError(err))
        shape = dp.getByIndex(index)
        if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
            return None, None, format_error_payload(ToolExecutionError(f"Shape at index {index} is not a form control"))
        return index, shape, None

    wanted = (name or "").strip()
    if not wanted:
        return None, None, format_error_payload(ToolExecutionError("Pass name or index to address the form control."))

    hits = []
    for i in range(dp.getCount()):
        shape = dp.getByIndex(i)
        if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
            continue
        model = shape.Control
        model_name = getattr(model, "Name", "") or ""
        shape_name = getattr(shape, "Name", "") or ""
        if model_name == wanted or shape_name == wanted:
            hits.append((i, shape))
    if len(hits) == 1:
        return hits[0][0], hits[0][1], None
    if not hits:
        return None, None, format_error_payload(ToolExecutionError(f"No form control named '{wanted}'."))
    hit_details = []
    for idx, shape in hits:
        m_label = getattr(shape.Control, "Label", "") or ""
        hit_details.append(f"index {idx} (label '{m_label}')")
    return None, None, format_error_payload(ToolExecutionError(f"Several form controls match '{wanted}': {', '.join(hit_details)}; pass index as well."))


def _coerce_items(raw: Any) -> list[str] | None:
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",")]
    return None


def _parse_date_default_value(raw: str) -> tuple[int, int, int] | None:
    from datetime import datetime

    s = raw.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return (dt.year, dt.month, dt.day)
        except ValueError:
            pass
    return None


def _items_or_error(tool: Any, kwargs: dict[str, Any]) -> tuple[list[str] | None, dict[str, Any] | None]:
    if "items" in kwargs and kwargs["items"] is not None:
        coerced_items = _coerce_items(kwargs["items"])
        if coerced_items is None:
            return None, tool._tool_error("Parameter 'items' must be a list of strings or comma-separated string.")
        return coerced_items, None
    return None, None


class _StackedDrawPageCursor:
    def __init__(self, dp: Any) -> None:
        self.margin_x: int = 5000
        self.gap: int = 400
        bottom = 800
        for i in range(dp.getCount()):
            s = dp.getByIndex(i)
            pos = s.getPosition()
            sz = s.getSize()
            bottom = max(bottom, pos.Y + sz.Height)
        self.next_y: int = bottom + self.gap

    def next_position(self, height: int) -> Point:
        y = self.next_y
        self.next_y = y + height + self.gap
        return Point(self.margin_x, y)


# One cursor per draw page. The active-page cursor supplied every field's Y, then the shape was added to field page=.
class _PageCursors:
    def __init__(self) -> None:
        self._entries: list[tuple[Any, _StackedDrawPageCursor]] = []

    def for_page(self, dp: Any) -> _StackedDrawPageCursor:
        for page, cursor in self._entries:
            if uno_same(page, dp):
                return cursor
        cursor = _StackedDrawPageCursor(dp)
        self._entries.append((dp, cursor))
        return cursor


def _form_page_cursors(doc: Any) -> _PageCursors | None:
    if _is_spreadsheet_doc(doc) or _is_draw_doc(doc):
        return _PageCursors()
    return None


def _next_stacked_position_on_draw_page(dp: Any, default_height: int) -> Point:
    """Place new controls below existing shapes on a draw page (1/100 mm)."""
    return _StackedDrawPageCursor(dp).next_position(default_height)


def _commit_form_batch(
    tool: Any,
    doc: Any,
    title: str,
    mutate: Any,
    undo_message: str,
) -> dict[str, Any] | None:
    from plugin.doc.undo import UndoUnavailable, commit_or_rollback

    try:
        error = commit_or_rollback(doc, title, mutate)
        if error is not None:
            error["rolled_back"] = True
            return error
    except UndoUnavailable:
        return tool._tool_error(undo_message, code="UNDO_UNAVAILABLE")
    return None


# XCell.getType() members that can take setString without dropping a number or formula.
_CALC_APPENDABLE_KINDS = frozenset({"EMPTY", "TEXT"})
_CALC_CELL_KINDS = _CALC_APPENDABLE_KINDS | frozenset({"VALUE", "FORMULA"})


def _calc_cell_content_kind(cell: Any) -> str:
    """Return EMPTY, TEXT, VALUE, FORMULA, or '' when the cell type is unknown.

    ``XCell.getType()`` is a ``CellContentType`` enum. PyUNO exposes the member
    name on ``.value`` (``'FORMULA'``). A plain string is accepted so a test
    double can stub the same check. Anything else is unknown: callers must not
    guess, because ``setString`` would replace whatever is really in the cell.
    """
    content_type = cell.getType()
    if content_type is None:
        return ""
    raw = getattr(content_type, "value", None)
    if not isinstance(raw, str) or not raw.strip():
        if isinstance(content_type, str):
            raw = content_type
        else:
            # uno.Enum repr when .value is absent:
            # <Enum instance com.sun.star.table.CellContentType ('FORMULA')>
            text = str(content_type)
            if "('" in text and text.endswith("')>"):
                raw = text[text.rfind("('") + 2 : -3]
            else:
                return ""
    name = raw.strip().upper()
    if name in _CALC_CELL_KINDS:
        return name
    return ""


def _append_text_to_calc_active_area(doc: Any, text: str) -> None:
    """Append *text* to the active Calc cell when that will not destroy it.

    ``getString()`` + ``setString()`` stores the displayed characters
    as text. A formula or number in the active cell becomes a
    literal, plus the form label or the space inserted after each
    control. ``FormCreate._insert_space`` and
    ``FormGenerate._insert_text`` both call this for spreadsheet
    documents. VALUE and FORMULA cells are left unchanged. EMPTY and
    TEXT cells still take the append, which is where a Calc form
    label belongs. An unknown type is left unchanged too — guessing
    would risk the same rewrite.
    """
    controller = doc.getCurrentController()
    sheet = controller.ActiveSheet
    selection = controller.getSelection()
    if selection is not None and hasattr(selection, "getRangeAddress"):
        addr = selection.getRangeAddress()
        cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
    elif selection is not None and hasattr(selection, "getRangeAddresses"):
        ranges = selection.getRangeAddresses()
        if ranges:
            # Why-comment: a multi-range has no getRangeAddress, and A1 is not that selection.
            addr = ranges[0]
            cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
        else:
            cell = sheet.getCellByPosition(0, 0)
    else:
        cell = sheet.getCellByPosition(0, 0)
    kind = _calc_cell_content_kind(cell)
    if kind not in _CALC_APPENDABLE_KINDS:
        return
    prev = ""
    if kind == "TEXT":
        prev = cell.getString() or ""
    cell.setString(prev + (text or ""))


def _field_failure_result(tool: Any, results: list[Any], *, total: int, action: str, names: list[Any] | None = None) -> dict[str, Any] | None:
    """Return an error payload when any form field failed, else None.

    ``form_create`` and ``form_generate`` must not return status ok
    after a control failed, or the caller treats a partial form as
    finished. The error payload repeats those field results and
    names the failures.
    """
    bits: list[str] = []
    for index, res in enumerate(results):
        if not isinstance(res, dict) or res.get("status") != "error":
            continue
        msg = str(res.get("message") or res.get("code") or "field failed")
        name = ""
        if names is not None and index < len(names):
            item = names[index]
            if isinstance(item, dict):
                name = str(item.get("name") or "")
            elif isinstance(item, str):
                name = item
        bits.append(f"{name}: {msg}" if name else msg)
    if not bits:
        return None
    shown = "; ".join(bits[:5])
    if len(bits) > 5:
        shown += "; ..."
    return tool._tool_error(f"{action}: {len(bits)} of {total} fields failed. {shown}", results=results)


def _plain_text_for_calc_html_fragment(html: str) -> str:
    """Rough strip of HTML for inserting generated form labels into a cell."""
    t = re.sub(r"(?is)<script.*?>.*?</script>", "", html)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_CONTROL_PROPERTIES: dict[str, Any] = {
    "control": enum_param(list(CONTROL_TYPE_MAP), "The type of form control to create."),
    "label": {"type": "string", "description": "Label text for the control (e.g. 'I agree')."},
    "name": {"type": "string", "description": "Internal name/key for the control."},
    "group_name": {"type": "string", "description": "Group name for radio buttons (mutually exclusive in the same group)."},
    "items": {"type": "array", "items": {"type": "string"}, "description": "List of options for a combobox or list box."},
    "placeholder": {"type": "string", "description": "Placeholder/hint text for text fields."},
    "default_value": {"type": "string", "description": "Initial value for text or date fields."},
    "width": {"type": "integer", "description": "Width in 100ths of mm (default varies by type)."},
    "height": {"type": "integer", "description": "Height in 100ths of mm (default varies by type)."},
}


_PAGE_PROPERTY = {"type": "integer", "description": "Draw/Impress 0-based page index (active page if omitted). Ignored in Writer/Calc."}


class FormCreateControl(ToolWriterFormBase):
    """Creates a single interactive form control at the current cursor position."""

    name: str | None = "form_create_control"
    uno_services: list[str] | None = _FORM_DOC_SERVICES
    description: str = (
        "Create one live LibreOffice form widget (checkbox, text field, radio, date, combobox, list box, or button) "
        "because the user asked for an interactive ControlShape. Writer: anchored As Character at the cursor. "
        "Calc: stacked on the active sheet draw page. Draw/Impress: stacked on the active page (or page=). "
        "Do not use this to fill paper-form blanks (empty TextShapes) — those are fill_draw_fields / shape_upsert targets."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            **_CONTROL_PROPERTIES,
            "page": _PAGE_PROPERTY,
        },
        "required": ["control", "name"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, page_cursors: Any = None, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        control_type = str(kwargs.get("control", ""))
        name = kwargs.get("name", "Field")
        label = kwargs.get("label", "")

        if control_type not in CONTROL_TYPE_MAP:
            legal = ", ".join(CONTROL_TYPE_MAP.keys())
            return self._tool_error(f"Unknown control type '{control_type}'. Allowed types: {legal}")

        if control_type == "date" and kwargs.get("default_value"):
            date_val = _parse_date_default_value(kwargs["default_value"])
            if date_val is None:
                return self._tool_error(f"Invalid date format for default_value '{kwargs['default_value']}'; expected DD.MM.YYYY or YYYY-MM-DD.")
        else:
            date_val = None

        coerced_items, items_err = _items_or_error(self, kwargs)
        if items_err:
            return items_err

        full_comp_name = CONTROL_TYPE_MAP[control_type]

        try:
            # Create control model
            model = doc.createInstance(full_comp_name)
            if not model:
                return format_error_payload(ToolExecutionError(f"Failed to create form component {full_comp_name}"))

            if hasattr(model, "Label"):
                model.Label = label

            # Type-specific settings
            if control_type == "text" and kwargs.get("placeholder"):
                if hasattr(model, "HelpText"):
                    model.HelpText = kwargs["placeholder"]

            if control_type == "text" and kwargs.get("default_value"):
                model.Text = kwargs["default_value"]

            if control_type == "date" and date_val is not None:
                year, month, day = date_val
                try:
                    import uno
                    d_struct = uno.createUnoStruct("com.sun.star.util.Date")
                    setattr(d_struct, "Year", year)
                    setattr(d_struct, "Month", month)
                    setattr(d_struct, "Day", day)
                    if hasattr(model, "Date"):
                        model.Date = d_struct
                    if hasattr(model, "DefaultDate"):
                        model.DefaultDate = d_struct
                except Exception:
                    date_long = int(f"{year:04d}{month:02d}{day:02d}")
                    if hasattr(model, "Date"):
                        model.Date = date_long
                    if hasattr(model, "DefaultDate"):
                        model.DefaultDate = date_long

            if control_type in ("combobox", "listbox") and coerced_items is not None:
                model.StringItemList = tuple(coerced_items)

            # Create the shape
            shape = doc.createInstance("com.sun.star.drawing.ControlShape")

            # Default sizes (100ths of mm)
            w = kwargs.get("width", 3000 if control_type not in ("checkbox", "radio") else 500)
            h = kwargs.get("height", 600 if control_type not in ("checkbox", "radio") else 500)
            shape.setSize(Size(w, h))

            shape.Control = model

            # Radios group by model Name; shape Name stays caller's name
            if control_type == "radio" and kwargs.get("group_name"):
                model.Name = kwargs["group_name"]
                shape.Name = name
                group_name = kwargs["group_name"]
            else:
                model.Name = name
                shape.Name = name
                group_name = getattr(model, "Name", "") or ""

            if _is_spreadsheet_doc(doc) or _is_draw_doc(doc):
                dp = _resolve_form_page(doc, kwargs.get("page"))
                if dp is None:
                    return _no_form_draw_page_payload()
                if page_cursors is not None:
                    pos = page_cursors.for_page(dp).next_position(h)
                else:
                    pos = _next_stacked_position_on_draw_page(dp, h)
                shape.setPosition(pos)
                dp.add(shape)
            else:
                # Anchor 'As Character' so it flows with text
                shape.setPropertyValue("AnchorType", AS_CHARACTER)
                from plugin.doc.text_helpers import selected_text_range

                anchor = selected_text_range(doc.getCurrentController())
                if anchor is None:
                    anchor = doc.getCurrentController().getViewCursor()
                # The anchor's XText owns that range.
                host = anchor.getText()
                host.insertTextContent(anchor, shape, False)

            return {
                "status": "ok",
                "message": f"Created {control_type} control '{name}'",
                "control_name": shape.Name,
                "group_name": group_name,
            }

        except Exception as e:
            if is_real_disposal(e):
                raise
            log.exception("Error creating form control")
            return format_error_payload(ToolExecutionError(f"Error creating form control: {str(e)}"))


class FormCreate(ToolWriterFormBase):
    """Fat API: Creates multiple form controls at once."""

    name: str | None = "form_create"
    uno_services: list[str] | None = _FORM_DOC_SERVICES
    description: str = (
        "Create several live form widgets in one call (Writer/Calc/Draw/Impress). "
        "Use this for interactive ControlShapes, not to fill empty paper-form text boxes."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        **_CONTROL_PROPERTIES,
                        "page": _PAGE_PROPERTY,
                    },
                    "required": ["control", "name"],
                },
            }
        },
        "required": ["fields"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        fields = kwargs["fields"]
        assert isinstance(fields, list)
        doc = ctx.doc
        results: list[Any] = []
        creator = FormCreateControl()
        page_cursors = _form_page_cursors(doc)

        def mutate() -> dict[str, Any] | None:
            for field in fields:
                res = creator._execute_main(ctx, page_cursors=page_cursors, **field)
                results.append(res)
                # Call _insert_space only when that field's result is not status="error"
                if isinstance(res, dict) and res.get("status") != "error":
                    self._insert_space(ctx)

            failure = _field_failure_result(self, results, total=len(fields), action="form_create", names=fields)
            if failure is not None:
                return failure
            return None

        batch_err = _commit_form_batch(
            self,
            doc,
            "Form Create",
            mutate,
            "Cannot create form controls atomically (no usable undo context).",
        )
        if batch_err is not None:
            return batch_err

        return {"status": "ok", "message": f"Processed {len(fields)} form fields", "results": results}

    def _insert_space(self, ctx: Any) -> None:
        doc = ctx.doc
        if _is_spreadsheet_doc(doc):
            _append_text_to_calc_active_area(doc, " ")
            return
        if _is_draw_doc(doc):
            return
        vc = doc.getCurrentController().getViewCursor()
        doc.getText().insertString(vc, " ", False)


class FormGenerate(ToolWriterFormBase):
    """Thin API: Generates a form from a description using a specialized internal prompt."""

    name: str | None = "form_generate"
    uno_services: list[str] | None = _FORM_DOC_SERVICES
    description: str = (
        "Generate a layout with interactive form widgets from a description. "
        "Writer: HTML at the cursor. Calc: plain text in the active cell area; widgets on the sheet draw page. "
        "Draw/Impress: labels as TextShapes and widgets stacked on the active page. "
        "Not a PDF/AcroForm fill — paper-form blanks stay empty TextShapes."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"description": {"type": "string", "description": "Description of the form to generate (e.g. 'Medical intake form')."}}, "required": ["description"]}

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.config import get_api_config
        from plugin.framework.client.llm_client import LlmClient

        description = kwargs["description"]
        config = get_api_config()
        client = LlmClient(config, ctx.ctx)

        # System instructions inspired by OnlyOfficeAI
        instructions = """Generate a document template in HTML format.
Use simple HTML tags like <h1>, <p>, <b>, <ul>, <li> for text and structure. For interactive input fields, use the special syntax:
{FIELD:control='type',name='uniqueName',label='Label',items='opt1,opt2',placeholder='hint'}

Available Field Types:
- checkbox: {FIELD:control='checkbox',name='key',label='Description'}
- text: {FIELD:control='text',name='key',placeholder='Hint'}
- radio: {FIELD:control='radio',name='optionKey',group_name='groupKey',label='Option'}
- date: {FIELD:control='date',name='key',default_value='DD.MM.YYYY'}
- combobox: {FIELD:control='combobox',name='key',items='opt1,opt2',label='Choose'}
- listbox: {FIELD:control='listbox',name='key',items='opt1,opt2',label='Choose'}
- button: {FIELD:control='button',name='key',label='Submit'}

Output ONLY the HTML content. No explanations. No Markdown like # Header.
"""

        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": f"Generate a {description}"}]

        try:
            if thread_guard.on_main_thread():
                return format_error_payload(ToolExecutionError("FormGenerate LLM call must not run on the UI thread"))

            # Get the full document from LLM
            content = client.chat_completion_sync(
                messages,
                max_tokens=2048,
                stop_checker=ctx.stop_checker,
                status_callback=ctx.status_callback,
            )

            # Process the content
            return run_on_main(self._process_form_content, ctx, content)

        except Exception as e:
            if is_real_disposal(e):
                raise
            if getattr(e, "code", None) == "USER_STOPPED":
                return format_error_payload(e)
            log.exception("Error in form_generate")
            return format_error_payload(ToolExecutionError(f"Form generation failed: {str(e)}"))

    def _process_form_content(self, ctx: Any, content: str) -> dict[str, Any]:
        doc = ctx.doc
        parts = re.split(r"(\{FIELD:[^\}]+\})", content)

        creator = FormCreateControl()
        field_results: list[Any] = []
        field_names: list[Any] = []

        page_cursors = _form_page_cursors(doc)

        def mutate() -> dict[str, Any] | None:
            for part in parts:
                if part.startswith("{FIELD:"):
                    params = self._parse_field_tag(part)
                    if params:
                        res = creator._execute_main(ctx, page_cursors=page_cursors, **params)
                        if not isinstance(res, dict):
                            res = format_error_payload(ToolExecutionError("Form field returned no result."))
                        field_results.append(res)
                        field_names.append(params)
                    else:
                        snippet = part if len(part) <= 80 else part[:77] + "..."
                        field_results.append(format_error_payload(ToolExecutionError(f"Could not parse form field tag: {snippet}")))
                        field_names.append({})
                else:
                    if part:
                        # A TypeError after a partial insert was inserting the text again and hiding the error.
                        self._insert_text(ctx, part, page_cursors=page_cursors)

            failure = _field_failure_result(self, field_results, total=len(field_results), action="form_generate", names=field_names)
            if failure is not None:
                return failure
            return None

        batch_err = _commit_form_batch(
            self,
            doc,
            "Form Generate",
            mutate,
            "Cannot generate form controls atomically (no usable undo context).",
        )
        if batch_err is not None:
            return batch_err

        return {"status": "ok", "message": "Form generation completed and inserted."}

    def _insert_text(self, ctx: Any, text: str, page_cursors: Any = None) -> None:
        doc = ctx.doc
        if _is_spreadsheet_doc(doc):
            plain = _plain_text_for_calc_html_fragment(text)
            if plain:
                _append_text_to_calc_active_area(doc, plain + " ")
            return
        if _is_draw_doc(doc):
            from plugin.draw.bridge import DrawBridge

            bridge = DrawBridge(doc)
            dp = bridge.get_active_page()
            plain = _plain_text_for_calc_html_fragment(text)
            if not plain:
                return
            # Create a text shape for the label/text
            w, h = 8000, 1000  # Default size for text labels
            if page_cursors is not None:
                pos = page_cursors.for_page(dp).next_position(h)
            else:
                pos = _next_stacked_position_on_draw_page(dp, h)
            shape = bridge.create_shape("com.sun.star.drawing.TextShape", pos.X, pos.Y, w, h, page=dp)
            shape.setString(plain)
            return

        from ..html_import import insert_html_fragment_at_cursor

        vc = doc.getCurrentController().getViewCursor()
        cursor_range = clone_text_range(vc)
        insert_html_fragment_at_cursor(cursor_range, text, wrap=False)

    def _parse_field_tag(self, tag: str) -> dict[str, Any] | None:
        pairs = re.findall(r"(\w+)[:=]['\"]([^'\"]*)['\"]", tag)
        params: dict[str, Any] = dict(pairs)
        if "control" not in params or "name" not in params:
            return None
        if params["control"] not in CONTROL_TYPE_MAP:
            return None
        if "items" in params:
            coerced = _coerce_items(params["items"])
            if coerced is None:
                return None
            params["items"] = coerced
        return params


class FormListControls(ToolWriterFormBase):
    """Lists all interactive form controls in the document."""

    name: str | None = "form_list_controls"
    uno_services: list[str] | None = _FORM_DOC_SERVICES
    description: str = (
        "List live form widgets (ControlShapes) with name, type, current text/State, and draw-page index "
        "so you can edit or delete by name. Writer: document draw page. Calc: active sheet only. "
        "Draw/Impress: active page, or page= for a specific slide. Index is the draw-page shape index — "
        "it shifts when non-controls sit between widgets; prefer name."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": _PAGE_PROPERTY,
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        dp = _resolve_form_page(doc, kwargs.get("page"))
        if dp is None:
            return _no_form_draw_page_payload()
        controls = []

        for i in range(dp.getCount()):
            try:
                shape = dp.getByIndex(i)
                if shape.getShapeType() == "com.sun.star.drawing.ControlShape":
                    model = shape.Control
                    shape_name = getattr(shape, "Name", "") or ""
                    model_name = getattr(model, "Name", "") or ""
                    ctrl_name = shape_name if shape_name else model_name
                    info = {"index": i, "name": ctrl_name, "type": _get_readable_type(model)}
                    if model_name and ctrl_name != model_name:
                        info["group_name"] = model_name
                    info.update(_control_value_fields(model))

                    # Geometry
                    pos = shape.getPosition()
                    sz = shape.getSize()
                    info["x"] = pos.X
                    info["y"] = pos.Y
                    info["width"] = sz.Width
                    info["height"] = sz.Height

                    controls.append(info)
            except Exception as e:
                reraise_if_disposed(e)
                controls.append({"index": i, "error": str(e), "stale": True})

        out: dict[str, Any] = {"status": "ok", "controls": controls, "count": len(controls)}
        if _is_spreadsheet_doc(doc):
            out["note"] = "Indices are ControlShapes on the active sheet draw page only."
        elif _is_draw_doc(doc):
            out["note"] = "Address controls by name; index is the draw-page shape index and shifts when other shapes sit between widgets."
        return out


class FormEditControl(ToolWriterFormBase):
    """Modifies properties of an existing form control."""

    name: str | None = "form_edit_control"
    uno_services: list[str] | None = _FORM_DOC_SERVICES
    description: str = (
        "Edit a live form widget by name (preferred) or draw-page index from form_list_controls. "
        "Set checkbox/radio State (0/1/2 or yes/no) and text-field Text. Writer/Calc/Draw/Impress. "
        "When index is omitted, name looks up the control; use new_name to rename. "
        "When index is passed, name still renames (older callers)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Draw-page shape index from form_list_controls (optional if name is set)."},
            "name": {"type": "string", "description": "Lookup Name when index is omitted; rename when index is passed."},
            "new_name": {"type": "string", "description": "Rename the control (use this when looking up by name)."},
            "label": {"type": "string", "description": "New label text."},
            "text": {"type": "string", "description": "New text value (for text fields)."},
            "state": {"type": "integer", "description": "Checkbox/radio State: 0 unchecked, 1 checked, 2 indeterminate (yes/no/checked strings also accepted)."},
            "items": {"type": "array", "items": {"type": "string"}, "description": "New item list (for comboboxes or list boxes)."},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "page": _PAGE_PROPERTY,
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        dp = _resolve_form_page(doc, kwargs.get("page"))
        if dp is None:
            return _no_form_draw_page_payload()

        lookup_name = None if "index" in kwargs else kwargs.get("name")
        idx, shape, err = _find_control_shape(dp, index=kwargs.get("index"), name=lookup_name)
        if err or shape is None:
            return err or format_error_payload(ToolExecutionError("Form control not found."))

        model = shape.Control

        coerced_items, items_err = _items_or_error(self, kwargs)
        if items_err:
            return items_err

        if "state" in kwargs:
            if not hasattr(model, "State"):
                return format_error_payload(ToolExecutionError("This control has no State (not a checkbox/radio)."))
            coerced_state = coerce_control_state(kwargs["state"])
            if coerced_state is None:
                return format_error_payload(ToolExecutionError(f"Could not interpret {kwargs['state']!r} as a checkbox/radio State."))
        else:
            coerced_state = None

        # Update Model and Shape
        if "new_name" in kwargs:
            model.Name = kwargs["new_name"]
            shape.Name = kwargs["new_name"]
        elif "index" in kwargs and "name" in kwargs:
            # index+name keeps the old "name means rename" meaning.
            model.Name = kwargs["name"]
            shape.Name = kwargs["name"]
        if "label" in kwargs and hasattr(model, "Label"):
            model.Label = kwargs["label"]
        if "text" in kwargs and hasattr(model, "Text"):
            model.Text = kwargs["text"]
        if coerced_state is not None:
            model.State = coerced_state
        if coerced_items is not None and hasattr(model, "StringItemList"):
            model.StringItemList = tuple(coerced_items)

        # Update Shape Geometry
        if any(k in kwargs for k in ["x", "y"]):
            pos = shape.getPosition()
            shape.setPosition(Point(kwargs.get("x", pos.X), kwargs.get("y", pos.Y)))

        if any(k in kwargs for k in ["width", "height"]):
            sz = shape.getSize()
            shape.setSize(Size(kwargs.get("width", sz.Width), kwargs.get("height", sz.Height)))

        values = _control_value_fields(model)
        return {"status": "ok", "message": f"Updated form control at index {idx}", "control_name": model.Name, "index": idx, **{k: values[k] for k in ("text", "state", "selected") if k in values}}


class FormDeleteControl(ToolWriterFormBase):
    """Deletes a form control by its index."""

    name: str | None = "form_delete_control"
    uno_services: list[str] | None = _FORM_DOC_SERVICES
    description: str = (
        "Delete a live form widget by name (preferred) or draw-page index. "
        "Writer/Calc/Draw/Impress. Index shifts when non-controls sit between widgets."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Draw-page shape index (optional if name is set)."},
            "name": {"type": "string", "description": "Control Name from form_list_controls."},
            "page": _PAGE_PROPERTY,
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        dp = _resolve_form_page(doc, kwargs.get("page"))
        if dp is None:
            return _no_form_draw_page_payload()
        idx, shape, err = _find_control_shape(dp, index=kwargs.get("index"), name=kwargs.get("name"))
        if err or shape is None:
            return err or format_error_payload(ToolExecutionError("Form control not found."))

        dp.remove(shape)

        return {"status": "ok", "message": f"Deleted form control at index {idx}", "index": idx}
