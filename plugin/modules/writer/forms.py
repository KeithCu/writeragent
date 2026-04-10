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
Form tools for Writer and Calc (shared registration: dual specialized bases + union uno_services).
Adapted from OnlyOfficeAI patterns. Original source: onlyofficeai/scripts/helpers/helpers.js (generateForm)
"""

import logging
import re
from com.sun.star.awt import Point, Size
from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

from plugin.modules.writer.base import ToolWriterFormBase
from plugin.framework.errors import format_error_payload, ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread

log = logging.getLogger("writeragent.writer.forms")

# One registration per tool name; union services for Writer + Calc (see AGENTS.md shared tools).
_FORM_DOC_SERVICES = [
    "com.sun.star.text.TextDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
]

_CONTROL_TYPE_MAP = {
    "checkbox": "com.sun.star.form.component.CheckBox",
    "text": "com.sun.star.form.component.TextField",
    "radio": "com.sun.star.form.component.RadioButton",
    "date": "com.sun.star.form.component.DateField",
    "combobox": "com.sun.star.form.component.ComboBox",
    "button": "com.sun.star.form.component.CommandButton",
}

def _get_readable_type(model):
    """Maps a UNO model back to a human-friendly type string."""
    for type_str, service in _CONTROL_TYPE_MAP.items():
        if model.supportsService(service):
            return type_str
    return "unknown"


def _is_spreadsheet_doc(doc) -> bool:
    if doc is None:
        return False
    try:
        return bool(doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"))
    except Exception:
        return False


def _get_form_draw_page(doc):
    """Writer: document draw page. Calc: active sheet draw page (indices are per active sheet)."""
    if _is_spreadsheet_doc(doc):
        from plugin.modules.calc.bridge import CalcBridge

        sheet = CalcBridge(doc).get_active_sheet()
        return sheet.getDrawPage()
    return doc.getDrawPage()


def _next_stacked_position_on_draw_page(dp, default_width: int, default_height: int) -> Point:
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


def _append_text_to_calc_active_area(doc, text: str) -> None:
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


class CreateFormControl(ToolWriterFormBase):
    """Creates a single interactive form control at the current cursor position."""

    name = "create_form_control"
    uno_services = _FORM_DOC_SERVICES
    description = (
        "Creates a single interactive form control (checkbox, text field, radio button, date field, combobox, or button). "
        "In Writer: anchored 'As Character' at the cursor. In Calc: placed on the active sheet draw page (stacked below existing shapes)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["checkbox", "text", "radio", "date", "combobox"],
                "description": "The type of form control to create.",
            },
            "label": {
                "type": "string",
                "description": "Label text for the control (e.g. 'I agree').",
            },
            "name": {
                "type": "string",
                "description": "Internal name/key for the control.",
            },
            "group_name": {
                "type": "string",
                "description": "Group name for radio buttons (mutually exclusive in the same group).",
            },
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of options for a combobox.",
            },
            "placeholder": {
                "type": "string",
                "description": "Placeholder/hint text for text fields.",
            },
            "default_value": {
                "type": "string",
                "description": "Initial value for text or date fields.",
            },
            "width": {
                "type": "integer",
                "description": "Width in 100ths of mm (default varies by type).",
            },
            "height": {
                "type": "integer",
                "description": "Height in 100ths of mm (default varies by type).",
            },
        },
        "required": ["type", "name"],
    }

    def execute(self, ctx, **kwargs):
        return execute_on_main_thread(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx, **kwargs):
        doc = ctx.doc
        control_type = str(kwargs.get("type", "text"))
        name = kwargs.get("name", "Field")
        label = kwargs.get("label", "")
        
        # Map control strings to UNO components
        component_map = {
            "text": "TextField",
            "checkbox": "CheckBox",
            "radio": "RadioButton",
            "date": "DateField",
            "combobox": "ComboBox",
            "button": "CommandButton"
        }
        
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

            if _is_spreadsheet_doc(doc):
                dp = _get_form_draw_page(doc)
                pos = _next_stacked_position_on_draw_page(dp, w, h)
                shape.setPosition(pos)
                dp.add(shape)
            else:
                # Anchor 'As Character' so it flows with text
                shape.setPropertyValue("AnchorType", AS_CHARACTER)
                text = doc.getText()
                selection = doc.getCurrentController().getSelection()
                if selection and selection.getCount() > 0:
                    anchor = selection.getByIndex(0)
                else:
                    anchor = doc.getCurrentController().getViewCursor()
                text.insertTextContent(anchor, shape, False)
            
            return {
                "status": "ok",
                "message": f"Created {control_type} control '{name}'",
                "control_name": name
            }
            
        except Exception as e:
            log.exception("Error creating form control")
            return format_error_payload(ToolExecutionError(f"Error creating form control: {str(e)}"))

class CreateForm(ToolWriterFormBase):
    """Fat API: Creates multiple form controls at once."""
    
    name = "create_form"
    uno_services = _FORM_DOC_SERVICES
    description = (
        "Creates multiple form controls at once from a list of field definitions. "
        "Useful for generating a complete form section in one call."
    )
    parameters = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["checkbox", "text", "radio", "date", "combobox"]},
                        "label": {"type": "string"},
                        "name": {"type": "string"},
                        "group_name": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "string"}},
                        "placeholder": {"type": "string"},
                        "default_value": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["type", "name"]
                }
            }
        },
        "required": ["fields"]
    }

    def execute(self, ctx, **kwargs):
        fields = kwargs.get("fields", [])
        results = []
        creator = CreateFormControl()
        for field in fields:
            res = creator._execute_main(ctx, **field)
            results.append(res)
            # Add a space after each control if we are inserting series
            execute_on_main_thread(self._insert_space, ctx)
            
        return {
            "status": "ok",
            "message": f"Processed {len(fields)} form fields",
            "results": results
        }
    
    def _insert_space(self, ctx):
        doc = ctx.doc
        if _is_spreadsheet_doc(doc):
            _append_text_to_calc_active_area(doc, " ")
            return
        vc = doc.getCurrentController().getViewCursor()
        doc.getText().insertString(vc, " ", False)

class GenerateForm(ToolWriterFormBase):
    """Thin API: Generates a form from a description using a specialized internal prompt."""
    
    name = "generate_form"
    uno_services = _FORM_DOC_SERVICES
    description = (
        "Generates a document or sheet layout with interactive form fields from a description. "
        "Writer: HTML inserted at the cursor. Calc: plain text is inserted into the active cell area; fields go on the active sheet draw page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Description of the form to generate (e.g. 'Medical intake form')."
            }
        },
        "required": ["description"]
    }

    def execute(self, ctx, **kwargs):
        from plugin.framework.config import get_api_config, get_config_int
        from plugin.modules.http.client import LlmClient
        
        description = kwargs.get("description")
        config = get_api_config(ctx.ctx)
        client = LlmClient(config, ctx.ctx)
        
        # System instructions inspired by OnlyOfficeAI
        instructions = """Generate a document template in HTML format.
Use simple HTML tags like <h1>, <p>, <b>, <ul>, <li> for text and structure. For interactive input fields, use the special syntax:
{FIELD:type='type',name='uniqueName',label='Label',items='opt1,opt2',placeholder='hint'}

Available Field Types:
- checkbox: {FIELD:type='checkbox',name='key',label='Description'}
- text: {FIELD:type='text',name='key',placeholder='Hint'}
- radio: {FIELD:type='radio',name='optionKey',group_name='groupKey',label='Option'}
- date: {FIELD:type='date',name='key',default_value='DD.MM.YYYY'}
- combobox: {FIELD:type='combobox',name='key',items='opt1,opt2',label='Choose'}
- button: {FIELD:type='button',name='key',label='Submit'}

Output ONLY the HTML content. No explanations. No Markdown like # Header.
"""
        
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"Generate a {description}"}
        ]
        
        try:
            # Get the full document from LLM
            content = client.chat_completion_sync(messages, max_tokens=2048)
            
            # Process the content
            return self._process_form_content(ctx, content)
            
        except Exception as e:
            log.exception("Error in generate_form")
            return format_error_payload(ToolExecutionError(f"Form generation failed: {str(e)}"))

    def _process_form_content(self, ctx, content):
        # We'll split the content by {FIELD:...} tags and insert parts
        parts = re.split(r'(\{FIELD:[^\}]+\})', content)
        
        creator = CreateFormControl()
        
        for part in parts:
            if part.startswith("{FIELD:"):
                # Parse the field tag
                params = self._parse_field_tag(part)
                if params:
                    execute_on_main_thread(creator._execute_main, ctx, **params)
            else:
                # Insert regular text
                if part:
                    execute_on_main_thread(self._insert_text, ctx, part)
        
        return {
            "status": "ok",
            "message": "Form generation completed and inserted."
        }

    def _insert_text(self, ctx, text):
        doc = ctx.doc
        if _is_spreadsheet_doc(doc):
            plain = _plain_text_for_calc_html_fragment(text)
            if plain:
                _append_text_to_calc_active_area(doc, plain + " ")
            return
        from plugin.modules.writer.ops import insert_html_at_cursor
        vc = doc.getCurrentController().getViewCursor()
        cursor = doc.getText().createTextCursorByRange(vc)
        insert_html_at_cursor(cursor, text)

    def _parse_field_tag(self, tag):
        # Extremely naive parser for {FIELD:type='...', ...}
        # Matches type='value' or type="value"
        pairs = re.findall(r"(\w+)[:=]['\"]([^'\"]*)['\"]", tag)
        params = dict(pairs)
        if "items" in params:
            params["items"] = [i.strip() for i in params["items"].split(",")]
        return params

class ListFormControls(ToolWriterFormBase):
    """Lists all interactive form controls in the document."""

    name = "list_form_controls"
    uno_services = _FORM_DOC_SERVICES
    description = (
        "Lists interactive form controls (checkboxes, text fields, etc.) with indices and values. "
        "Writer: document draw page. Calc: active sheet draw page only."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        return execute_on_main_thread(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx, **kwargs):
        doc = ctx.doc
        dp = _get_form_draw_page(doc)
        controls = []
        
        for i in range(dp.getCount()):
            shape = dp.getByIndex(i)
            if shape.getShapeType() == "com.sun.star.drawing.ControlShape":
                model = shape.Control
                info = {
                    "index": i,
                    "name": getattr(model, "Name", ""),
                    "type": _get_readable_type(model),
                }
                if hasattr(model, "Label"):
                    info["label"] = model.Label
                if hasattr(model, "Text"):
                    info["text"] = model.Text
                if hasattr(model, "StringItemList"):
                    info["items"] = list(model.StringItemList)
                
                # Geometry
                pos = shape.getPosition()
                sz = shape.getSize()
                info["x"] = pos.X
                info["y"] = pos.Y
                info["width"] = sz.Width
                info["height"] = sz.Height
                
                controls.append(info)
        
        out: dict = {
            "status": "ok",
            "controls": controls,
            "count": len(controls),
        }
        if _is_spreadsheet_doc(doc):
            out["note"] = "Indices are ControlShapes on the active sheet draw page only."
        return out

class EditFormControl(ToolWriterFormBase):
    """Modifies properties of an existing form control."""

    name = "edit_form_control"
    uno_services = _FORM_DOC_SERVICES
    description = (
        "Modifies an existing form control by index (from list_form_controls). "
        "Calc: index is on the active sheet draw page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "shape_index": {"type": "integer", "description": "The index of the control shape (from list_form_controls)."},
            "name": {"type": "string", "description": "New internal name."},
            "label": {"type": "string", "description": "New label text."},
            "text": {"type": "string", "description": "New text value (for text fields)."},
            "items": {"type": "array", "items": {"type": "string"}, "description": "New item list (for comboboxes)."},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
        },
        "required": ["shape_index"],
    }

    def execute(self, ctx, **kwargs):
        return execute_on_main_thread(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx, **kwargs):
        doc = ctx.doc
        dp = _get_form_draw_page(doc)
        idx = kwargs["shape_index"]
        
        if idx < 0 or idx >= dp.getCount():
            return format_error_payload(ToolExecutionError(f"Invalid shape index: {idx}"))
        
        shape = dp.getByIndex(idx)
        if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
            return format_error_payload(ToolExecutionError(f"Shape at index {idx} is not a form control"))
        
        model = shape.Control
        
        # Update Model
        if "name" in kwargs:
            model.Name = kwargs["name"]
        if "label" in kwargs and hasattr(model, "Label"):
            model.Label = kwargs["label"]
        if "text" in kwargs and hasattr(model, "Text"):
            model.Text = kwargs["text"]
        if "items" in kwargs and hasattr(model, "StringItemList"):
            model.StringItemList = tuple(kwargs["items"])
            
        # Update Shape Geometry
        if any(k in kwargs for k in ["x", "y"]):
            pos = shape.getPosition()
            shape.setPosition(Point(kwargs.get("x", pos.X), kwargs.get("y", pos.Y)))
        
        if any(k in kwargs for k in ["width", "height"]):
            sz = shape.getSize()
            shape.setSize(Size(kwargs.get("width", sz.Width), kwargs.get("height", sz.Height)))
            
        return {
            "status": "ok",
            "message": f"Updated form control at index {idx}",
            "control_name": model.Name
        }

class DeleteFormControl(ToolWriterFormBase):
    """Deletes a form control by its index."""

    name = "delete_form_control"
    uno_services = _FORM_DOC_SERVICES
    description = "Deletes a form control by shape index (Calc: active sheet draw page)."
    parameters = {
        "type": "object",
        "properties": {
            "shape_index": {"type": "integer", "description": "The index of the control shape to delete."}
        },
        "required": ["shape_index"]
    }

    def execute(self, ctx, **kwargs):
        return execute_on_main_thread(self._execute_main, ctx, **kwargs)

    def _execute_main(self, ctx, **kwargs):
        doc = ctx.doc
        dp = _get_form_draw_page(doc)
        idx = kwargs["shape_index"]
        
        if idx < 0 or idx >= dp.getCount():
            return format_error_payload(ToolExecutionError(f"Invalid shape index: {idx}"))
        
        shape = dp.getByIndex(idx)
        if shape.getShapeType() != "com.sun.star.drawing.ControlShape":
            return format_error_payload(ToolExecutionError(f"Shape at index {idx} is not a form control"))
        
        dp.remove(shape)
        
        return {
            "status": "ok",
            "message": f"Deleted form control at index {idx}"
        }
