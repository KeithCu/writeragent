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

from ..specialized_base import ToolWriterFormBase
from plugin.doc.doc_type import is_calc, is_draw
from plugin.doc.text_helpers import clone_text_range
from plugin.doc.visual_helpers import get_active_draw_page
from plugin.draw.tree import coerce_control_state
from plugin.framework.errors import format_error_payload, ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.framework.thread_guard import on_main_thread

def _run_on_main(fn: Any, *args: Any, **kwargs: Any):
    if on_main_thread():
        return fn(*args, **kwargs)
    return execute_on_main_thread(fn, *args, **kwargs)

log = logging.getLogger("writeragent.writer.forms")

# One registration per tool name; union services for Writer + Calc + Draw/Impress (see AGENTS.md shared tools).
_FORM_DOC_SERVICES = ["com.sun.star.text.TextDocument", "com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]

_CONTROL_TYPE_MAP = {
    "checkbox": "com.sun.star.form.component.CheckBox",
    "text": "com.sun.star.form.component.TextField",
    "radio": "com.sun.star.form.component.RadioButton",
    "date": "com.sun.star.form.component.DateField",
    "combobox": "com.sun.star.form.component.ComboBox",
    "button": "com.sun.star.form.component.CommandButton",
}


def _get_readable_type(model: Any):
    """Maps a UNO model back to a human-friendly type string."""
    for type_str, service in _CONTROL_TYPE_MAP.items():
        if model.supportsService(service):
            return type_str
    return "unknown"


# Local aliases keep call sites short.
_is_spreadsheet_doc = is_calc
_is_draw_doc = is_draw
_get_form_draw_page = get_active_draw_page


def _no_form_draw_page_payload():
    return format_error_payload(ToolExecutionError("No draw page available for form operations."))


def _resolve_form_page(doc: Any, page: Any = None):
    """Active draw page, or a Draw/Impress page index when *page* is set.

    Writer/Calc ignore *page* (Writer has one canvas; Calc uses the active sheet).
    """
    if page is not None and _is_draw_doc(doc):
        try:
            from plugin.draw.bridge import DrawBridge

            return DrawBridge(doc).get_pages().getByIndex(int(page))
        except Exception:
            return None
    return _get_form_draw_page(doc)


def _control_value_fields(model: Any):
    """Current value/state for list/edit so Draw widgets are visible without guessing."""
    info = {}
    if hasattr(model, "Label"):
        info["label"] = model.Label
    if hasattr(model, "Text"):
        info["text"] = model.Text
    if hasattr(model, "State"):
        try:
            info["state"] = int(model.State)
        except Exception:
            pass
    if hasattr(model, "StringItemList"):
        info["items"] = list(model.StringItemList)
    if hasattr(model, "SelectedItems"):
        try:
            info["selected"] = list(model.SelectedItems)
        except Exception:
            pass
    return info


def _find_control_shape(dp: Any, index: Any = None, name: Any = None):
    """Resolve a ControlShape by draw-page index or control/shape Name.

    Index is the draw-page shape index from form_list_controls (not a
    control-only ordinal). Non-controls between widgets shift that index;
    Name is the stable address.
    """
    if index is not None:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return None, None, format_error_payload(ToolExecutionError(f"Invalid shape index: {index}"))
        if idx < 0 or idx >= dp.getCount():
            return None, None, format_error_payload(ToolExecutionError(f"Invalid shape index: {idx}"))
        shape = dp.getByIndex(idx)
        if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
            return None, None, format_error_payload(ToolExecutionError(f"Shape at index {idx} is not a form control"))
        return idx, shape, None

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
    return None, None, format_error_payload(ToolExecutionError(f"Several form controls named '{wanted}'; pass index as well."))


def _apply_control_state(model: Any, state_value: Any):
    if not hasattr(model, "State"):
        return format_error_payload(ToolExecutionError("This control has no State (not a checkbox/radio)."))
    coerced = coerce_control_state(state_value)
    if coerced is None:
        return format_error_payload(ToolExecutionError(f"Could not interpret {state_value!r} as a checkbox/radio State."))
    model.State = coerced
    return None


def _next_stacked_position_on_draw_page(dp: Any, default_width: int, default_height: int) -> Point:
    """Place new controls below existing shapes on a draw page (1/100 mm)."""
    margin_x = 5000
    gap = 400
    max_bottom = 800
    for i in range(dp.getCount()):
        try:
            s = dp.getByIndex(i)
            pos = s.getPosition()
            sz = s.getSize()
            max_bottom = max(max_bottom, pos.Y + sz.Height)
        except Exception:
            continue
    return Point(margin_x, max_bottom + gap)


def _append_text_to_calc_active_area(doc: Any, text: str) -> None:
    controller = doc.getCurrentController()
    sheet = controller.ActiveSheet
    selection = controller.getSelection()
    if selection is not None and hasattr(selection, "getRangeAddress"):
        addr = selection.getRangeAddress()
        cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
    else:
        cell = sheet.getCellByPosition(0, 0)
    prev = cell.getString() or ""
    cell.setString(prev + (text or ""))


def _plain_text_for_calc_html_fragment(html: str) -> str:
    """Rough strip of HTML for inserting generated form labels into a cell."""
    t = re.sub(r"(?is)<script.*?>.*?</script>", "", html)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class FormCreateControl(ToolWriterFormBase):
    """Creates a single interactive form control at the current cursor position."""

    name: str | None = "form_create_control"
    uno_services: list = _FORM_DOC_SERVICES
    description: str = (
        "Create one live LibreOffice form widget (checkbox, text field, radio, date, combobox, or button) "
        "because the user asked for an interactive ControlShape. Writer: anchored As Character at the cursor. "
        "Calc: stacked on the active sheet draw page. Draw/Impress: stacked on the active page (or page=). "
        "Do not use this to fill paper-form blanks (empty TextShapes) — those are fill_draw_fields / shape_upsert targets."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "control": {"type": "string", "enum": ["checkbox", "text", "radio", "date", "combobox"], "description": "The type of form control to create."},
            "label": {"type": "string", "description": "Label text for the control (e.g. 'I agree')."},
            "name": {"type": "string", "description": "Internal name/key for the control."},
            "group_name": {"type": "string", "description": "Group name for radio buttons (mutually exclusive in the same group)."},
            "items": {"type": "array", "items": {"type": "string"}, "description": "List of options for a combobox."},
            "placeholder": {"type": "string", "description": "Placeholder/hint text for text fields."},
            "default_value": {"type": "string", "description": "Initial value for text or date fields."},
            "width": {"type": "integer", "description": "Width in 100ths of mm (default varies by type)."},
            "height": {"type": "integer", "description": "Height in 100ths of mm (default varies by type)."},
            "page": {"type": "integer", "description": "Draw/Impress 0-based page index (active page if omitted). Ignored in Writer/Calc."},
        },
        "required": ["control", "name"],
    }

    def execute(self, ctx: Any, **kwargs: Any):
        return _run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, **kwargs: Any):
        doc = ctx.doc
        control_type = str(kwargs.get("control", "text"))
        name = kwargs.get("name", "Field")
        label = kwargs.get("label", "")

        # Map control strings to UNO components
        component_map = {"text": "TextField", "checkbox": "CheckBox", "radio": "RadioButton", "date": "DateField", "combobox": "ComboBox", "button": "CommandButton"}

        comp_name = component_map.get(control_type, "TextField")
        full_comp_name = f"com.sun.star.form.component.{comp_name}"

        try:
            # Create control model
            model = doc.createInstance(full_comp_name)
            if not model:
                return format_error_payload(ToolExecutionError(f"Failed to create form component {full_comp_name}"))

            model.Name = name
            if hasattr(model, "Label"):
                model.Label = label

            # Type-specific settings
            if control_type == "text" and kwargs.get("placeholder"):
                if hasattr(model, "HelpText"):
                    model.HelpText = kwargs["placeholder"]

            if control_type == "text" and kwargs.get("default_value"):
                model.Text = kwargs["default_value"]

            if control_type == "combobox" and kwargs.get("items"):
                model.StringItemList = tuple(kwargs["items"])

            if control_type == "radio" and kwargs.get("group_name"):
                # In LibreOffice, radio buttons are grouped by having the same Name
                # but we can also set additional grouping properties if needed.
                # Actually, standard LO grouping for forms is by Name.
                model.Name = kwargs["group_name"]

            # Create the shape
            shape = doc.createInstance("com.sun.star.drawing.ControlShape")

            # Default sizes (100ths of mm)
            w = kwargs.get("width", 3000 if control_type != "checkbox" else 500)
            h = kwargs.get("height", 600 if control_type != "checkbox" else 500)
            shape.setSize(Size(w, h))

            shape.Control = model

            if _is_spreadsheet_doc(doc) or _is_draw_doc(doc):
                dp = _resolve_form_page(doc, kwargs.get("page"))
                if dp is None:
                    return _no_form_draw_page_payload()
                pos = _next_stacked_position_on_draw_page(dp, w, h)
                shape.setPosition(pos)
                dp.add(shape)
            else:
                # Anchor 'As Character' so it flows with text
                shape.setPropertyValue("AnchorType", AS_CHARACTER)
                text = doc.getText()
                selection = doc.getCurrentController().getSelection()
                if selection and hasattr(selection, "getCount") and selection.getCount() > 0:
                    anchor = selection.getByIndex(0)
                else:
                    anchor = doc.getCurrentController().getViewCursor()
                text.insertTextContent(anchor, shape, False)

            return {"status": "ok", "message": f"Created {control_type} control '{name}'", "control_name": name}

        except Exception as e:
            log.exception("Error creating form control")
            return format_error_payload(ToolExecutionError(f"Error creating form control: {str(e)}"))


class FormCreate(ToolWriterFormBase):
    """Fat API: Creates multiple form controls at once."""

    name: str | None = "form_create"
    uno_services: list = _FORM_DOC_SERVICES
    description: str = (
        "Create several live form widgets in one call (Writer/Calc/Draw/Impress). "
        "Use this for interactive ControlShapes, not to fill empty paper-form text boxes."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "control": {"type": "string", "enum": ["checkbox", "text", "radio", "date", "combobox"]},
                        "label": {"type": "string"},
                        "name": {"type": "string"},
                        "group_name": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "string"}},
                        "placeholder": {"type": "string"},
                        "default_value": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["control", "name"],
                },
            }
        },
        "required": ["fields"],
    }

    def execute(self, ctx: Any, **kwargs: Any):
        fields = kwargs.get("fields", [])
        results = []
        creator = FormCreateControl()
        for field in fields:
            res = creator._execute_main(ctx, **field)
            results.append(res)
            # Add a space after each control if we are inserting series
            _run_on_main(self._insert_space, ctx)

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
    uno_services: list = _FORM_DOC_SERVICES
    description: str = (
        "Generate a layout with interactive form widgets from a description. "
        "Writer: HTML at the cursor. Calc: plain text in the active cell area; widgets on the sheet draw page. "
        "Draw/Impress: labels as TextShapes and widgets stacked on the active page. "
        "Not a PDF/AcroForm fill — paper-form blanks stay empty TextShapes."
    )
    parameters: dict | None = {"type": "object", "properties": {"description": {"type": "string", "description": "Description of the form to generate (e.g. 'Medical intake form')."}}, "required": ["description"]}

    def execute(self, ctx: Any, **kwargs: Any):
        from plugin.framework.config import get_api_config
        from plugin.framework.client.llm_client import LlmClient

        description = kwargs.get("description")
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
- button: {FIELD:control='button',name='key',label='Submit'}

Output ONLY the HTML content. No explanations. No Markdown like # Header.
"""

        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": f"Generate a {description}"}]

        try:
            # Get the full document from LLM
            content = client.chat_completion_sync(messages, max_tokens=2048)

            # Process the content
            return self._process_form_content(ctx, content)

        except Exception as e:
            log.exception("Error in form_generate")
            return format_error_payload(ToolExecutionError(f"Form generation failed: {str(e)}"))

    def _process_form_content(self, ctx: Any, content: str):
        # We'll split the content by {FIELD:...} tags and insert parts
        parts = re.split(r"(\{FIELD:[^\}]+\})", content)

        creator = FormCreateControl()

        for part in parts:
            if part.startswith("{FIELD:"):
                # Parse the field tag
                params = self._parse_field_tag(part)
                if params:
                    _run_on_main(creator._execute_main, ctx, **params)
            else:
                # Insert regular text
                if part:
                    _run_on_main(self._insert_text, ctx, part)

        return {"status": "ok", "message": "Form generation completed and inserted."}

    def _insert_text(self, ctx: Any, text: str) -> None:
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
            pos = _next_stacked_position_on_draw_page(dp, w, h)
            shape = bridge.create_shape("com.sun.star.drawing.TextShape", pos.X, pos.Y, w, h, page=dp)
            shape.setString(plain)
            return

        from ..html_import import insert_html_fragment_at_cursor

        vc = doc.getCurrentController().getViewCursor()
        cursor = clone_text_range(vc)
        insert_html_fragment_at_cursor(cursor, text, wrap=False)

    def _parse_field_tag(self, tag: str):
        # Naive parser for {FIELD:control='...', ...}
        pairs = re.findall(r"(\w+)[:=]['\"]([^'\"]*)['\"]", tag)
        params = dict(pairs)
        if "items" in params:
            params["items"] = [i.strip() for i in params["items"].split(",")]
        return params


class FormListControls(ToolWriterFormBase):
    """Lists all interactive form controls in the document."""

    name: str | None = "form_list_controls"
    uno_services: list = _FORM_DOC_SERVICES
    description: str = (
        "List live form widgets (ControlShapes) with name, type, current text/State, and draw-page index "
        "so you can edit or delete by name. Writer: document draw page. Calc: active sheet only. "
        "Draw/Impress: active page, or page= for a specific slide. Index is the draw-page shape index — "
        "it shifts when non-controls sit between widgets; prefer name."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "Draw/Impress 0-based page index (active page if omitted). Ignored in Writer/Calc."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any):
        return _run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, **kwargs: Any):
        doc = ctx.doc
        dp = _resolve_form_page(doc, kwargs.get("page"))
        if dp is None:
            return _no_form_draw_page_payload()
        controls = []

        for i in range(dp.getCount()):
            shape = dp.getByIndex(i)
            if shape.getShapeType() == "com.sun.star.drawing.ControlShape":
                model = shape.Control
                info = {"index": i, "name": getattr(model, "Name", ""), "type": _get_readable_type(model)}
                info.update(_control_value_fields(model))

                # Geometry
                pos = shape.getPosition()
                sz = shape.getSize()
                info["x"] = pos.X
                info["y"] = pos.Y
                info["width"] = sz.Width
                info["height"] = sz.Height

                controls.append(info)

        out: dict = {"status": "ok", "controls": controls, "count": len(controls)}
        if _is_spreadsheet_doc(doc):
            out["note"] = "Indices are ControlShapes on the active sheet draw page only."
        elif _is_draw_doc(doc):
            out["note"] = "Address controls by name; index is the draw-page shape index and shifts when other shapes sit between widgets."
        return out


class FormEditControl(ToolWriterFormBase):
    """Modifies properties of an existing form control."""

    name: str | None = "form_edit_control"
    uno_services: list = _FORM_DOC_SERVICES
    description: str = (
        "Edit a live form widget by name (preferred) or draw-page index from form_list_controls. "
        "Set checkbox/radio State (0/1/2 or yes/no) and text-field Text. Writer/Calc/Draw/Impress. "
        "When index is omitted, name looks up the control; use new_name to rename. "
        "When index is passed, name still renames (older callers)."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Draw-page shape index from form_list_controls (optional if name is set)."},
            "name": {"type": "string", "description": "Lookup Name when index is omitted; rename when index is passed."},
            "new_name": {"type": "string", "description": "Rename the control (use this when looking up by name)."},
            "label": {"type": "string", "description": "New label text."},
            "text": {"type": "string", "description": "New text value (for text fields)."},
            "state": {"type": "integer", "description": "Checkbox/radio State: 0 unchecked, 1 checked, 2 indeterminate (yes/no/checked strings also accepted)."},
            "items": {"type": "array", "items": {"type": "string"}, "description": "New item list (for comboboxes)."},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "page": {"type": "integer", "description": "Draw/Impress 0-based page index (active page if omitted). Ignored in Writer/Calc."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any):
        return _run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, **kwargs: Any):
        doc = ctx.doc
        dp = _resolve_form_page(doc, kwargs.get("page"))
        if dp is None:
            return _no_form_draw_page_payload()

        lookup_name = None if "index" in kwargs else kwargs.get("name")
        idx, shape, err = _find_control_shape(dp, index=kwargs.get("index"), name=lookup_name)
        if err or shape is None:
            return err or format_error_payload(ToolExecutionError("Form control not found."))

        model = shape.Control

        # Update Model. index+name keeps the old "name means rename" meaning.
        if "new_name" in kwargs:
            model.Name = kwargs["new_name"]
        elif "index" in kwargs and "name" in kwargs:
            model.Name = kwargs["name"]
        if "label" in kwargs and hasattr(model, "Label"):
            model.Label = kwargs["label"]
        if "text" in kwargs and hasattr(model, "Text"):
            model.Text = kwargs["text"]
        if "state" in kwargs:
            state_err = _apply_control_state(model, kwargs["state"])
            if state_err:
                return state_err
        if "items" in kwargs and hasattr(model, "StringItemList"):
            model.StringItemList = tuple(kwargs["items"])

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
    uno_services: list = _FORM_DOC_SERVICES
    description: str = (
        "Delete a live form widget by name (preferred) or draw-page index. "
        "Writer/Calc/Draw/Impress. Index shifts when non-controls sit between widgets."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Draw-page shape index (optional if name is set)."},
            "name": {"type": "string", "description": "Control Name from form_list_controls."},
            "page": {"type": "integer", "description": "Draw/Impress 0-based page index (active page if omitted). Ignored in Writer/Calc."},
        },
        "required": [],
    }

    def execute(self, ctx: Any, **kwargs: Any):
        return _run_on_main(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx: Any, **kwargs: Any):
        doc = ctx.doc
        dp = _resolve_form_page(doc, kwargs.get("page"))
        if dp is None:
            return _no_form_draw_page_payload()
        idx, shape, err = _find_control_shape(dp, index=kwargs.get("index"), name=kwargs.get("name"))
        if err or shape is None:
            return err or format_error_payload(ToolExecutionError("Form control not found."))

        dp.remove(shape)

        return {"status": "ok", "message": f"Deleted form control at index {idx}", "index": idx}
