# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
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
import json
from plugin.framework.logging import log
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test


_test_doc = None
_test_ctx = None


@setup
def setup_impress_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )

    _test_doc = desktop.loadComponentFromURL("private:factory/simpress", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create Impress document"
    assert hasattr(_test_doc, "getDrawPages"), "Not a valid Impress document"

    log.info("[ImpressTests] impress_tests: starting tests")


@teardown
def teardown_impress_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _exec_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool_context import ToolContext
    tctx = ToolContext(_test_doc, _test_ctx, "impress", get_services(), "test")
    res = get_tools().execute(name, tctx, **args)
    return json.dumps(res) if isinstance(res, dict) else res



@native_test
def test_slide_transitions():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    # Initial transition state
    result = _exec_tool("get_slide_transition", {"page_index": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_transition failed: {result}"

    # Set a transition
    result = _exec_tool("set_slide_transition", {
        "page_index": 0,
        "effect": "fade_from_left",
        "speed": "fast",
        "duration": 5,
        "transition_duration": 1.5,
        "advance": "auto"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_slide_transition failed: {result}"

    # Verify the transition is set correctly
    result = _exec_tool("get_slide_transition", {"page_index": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_transition failed: {result}"
    assert data.get("effect") == "fade_from_left", f"Effect mismatch: {data.get('effect')}"
    assert data.get("speed") == "fast", f"Speed mismatch: {data.get('speed')}"
    assert data.get("duration") == 5, f"Duration mismatch: {data.get('duration')}"
    assert data.get("advance") == "auto", f"Advance mismatch: {data.get('advance')}"


@native_test
def test_speaker_notes():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    # Set new notes
    result = _exec_tool("set_speaker_notes", {
        "page_index": 0,
        "text": "These are my speaker notes for the first slide."
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_speaker_notes failed: {result}"

    # Append to notes
    result = _exec_tool("set_speaker_notes", {
        "page_index": 0,
        "text": "And some more notes.",
        "append": True
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_speaker_notes append failed: {result}"

    # Get notes and verify
    result = _exec_tool("get_speaker_notes", {"page_index": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_speaker_notes failed: {result}"
    notes = data.get("notes")
    assert "These are my speaker notes for the first slide." in notes, f"Expected notes not found: {notes}"
    assert "And some more notes." in notes, f"Expected appended notes not found: {notes}"
