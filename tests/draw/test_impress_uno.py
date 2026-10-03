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
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _exec_tool(doc, ctx, name, args):
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type="impress")
    return json.dumps(res) if isinstance(res, dict) else res



@native_test
@with_native_doc("impress")
def test_slide_transitions(ctx, doc):
    # Initial transition state
    result = _exec_tool(doc, ctx, "get_slide_transition", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_transition failed: {result}"

    # Set a transition
    result = _exec_tool(doc, ctx, "set_slide_transition", {
        "page": 0,
        "effect": "fade_from_left",
        "speed": "fast",
        "duration": 5,
        "transition_duration": 1.5,
        "advance": "auto"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_slide_transition failed: {result}"

    # Verify the transition is set correctly
    result = _exec_tool(doc, ctx, "get_slide_transition", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_slide_transition failed: {result}"
    assert data.get("effect") == "fade_from_left", f"Effect mismatch: {data.get('effect')}"
    assert data.get("speed") == "fast", f"Speed mismatch: {data.get('speed')}"
    assert data.get("duration") == 5, f"Duration mismatch: {data.get('duration')}"
    assert data.get("advance") == "auto", f"Advance mismatch: {data.get('advance')}"


@native_test
@with_native_doc("impress")
def test_speaker_notes(ctx, doc):
    # Set new notes
    result = _exec_tool(doc, ctx, "set_speaker_notes", {
        "page": 0,
        "text": "These are my speaker notes for the first slide."
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_speaker_notes failed: {result}"

    # Append to notes
    result = _exec_tool(doc, ctx, "set_speaker_notes", {
        "page": 0,
        "text": "And some more notes.",
        "append": True
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"set_speaker_notes append failed: {result}"

    # Get notes and verify
    result = _exec_tool(doc, ctx, "get_speaker_notes", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_speaker_notes failed: {result}"
    notes = data.get("notes")
    assert "These are my speaker notes for the first slide." in notes, f"Expected notes not found: {notes}"
    assert "And some more notes." in notes, f"Expected appended notes not found: {notes}"


def _collect_shape_marks(shape, texts, groups):
    try:
        shape_type = shape.getShapeType()
    except Exception:
        shape_type = ""
    if "GroupShape" in shape_type:
        count = shape.getCount()
        groups.append(count)
        for i in range(count):
            _collect_shape_marks(shape.getByIndex(i), texts, groups)
        return
    try:
        texts.append(shape.getString())
    except Exception:
        pass


def _impress_group_children(page):
    texts = []
    groups = []
    for i in range(page.getCount()):
        _collect_shape_marks(page.getByIndex(i), texts, groups)
    return texts, groups


@native_test
@with_native_doc("impress")
def test_move_slide_keeps_group_notes_and_layout(ctx, doc):
    """Move keeps the duplicated page's group, notes, layout, and transition."""
    layout = _exec_tool(doc, ctx, "set_slide_layout", {"page": 0, "layout": "title_only"})
    assert json.loads(layout).get("status") == "ok", layout
    notes = _exec_tool(doc, ctx, "set_speaker_notes", {"page": 0, "text": "keep-these-notes"})
    assert json.loads(notes).get("status") == "ok", notes
    transition = _exec_tool(doc, ctx, "set_slide_transition", {
        "page": 0,
        "effect": "dissolve",
        "speed": "fast",
    })
    assert json.loads(transition).get("status") == "ok", transition

    page0 = doc.getDrawPages().getByIndex(0)
    before = page0.getCount()
    for label, x in (("g-left", 1000), ("g-right", 5000)):
        created = _exec_tool(doc, ctx, "shape_upsert", {
            "action": "create",
            "shape_type": "rectangle",
            "page": 0,
            "x": x, "y": 3000, "width": 2000, "height": 1500,
            "text": label,
        })
        assert json.loads(created).get("status") == "ok", created
    grouped = _exec_tool(doc, ctx, "shape_group", {"page": 0, "indices": [before, before + 1]})
    assert json.loads(grouped).get("status") == "ok", grouped

    added = _exec_tool(doc, ctx, "add_slide", {})
    assert json.loads(added).get("status") == "ok", added
    other = _exec_tool(doc, ctx, "set_slide_layout", {"page": 1, "layout": "blank"})
    assert json.loads(other).get("status") == "ok", other

    moved = _exec_tool(doc, ctx, "move_slide", {"from_page": 0, "to_page": 1})
    assert json.loads(moved).get("status") == "ok", moved
    dest = doc.getDrawPages().getByIndex(1)
    texts, groups = _impress_group_children(dest)
    assert "g-left" in texts and "g-right" in texts, texts
    assert 2 in groups, groups
    got_notes = json.loads(_exec_tool(doc, ctx, "get_speaker_notes", {"page": 1}))
    assert got_notes.get("status") == "ok", got_notes
    assert "keep-these-notes" in (got_notes.get("notes") or ""), got_notes
    left_behind = json.loads(_exec_tool(doc, ctx, "get_speaker_notes", {"page": 0}))
    assert "keep-these-notes" not in (left_behind.get("notes") or ""), left_behind
    got_layout = json.loads(_exec_tool(doc, ctx, "get_slide_layout", {"page": 1}))
    assert got_layout.get("layout_name") == "title_only", got_layout
    stayed = json.loads(_exec_tool(doc, ctx, "get_slide_layout", {"page": 0}))
    assert stayed.get("layout_name") == "blank", stayed
    got_fx = json.loads(_exec_tool(doc, ctx, "get_slide_transition", {"page": 1}))
    assert got_fx.get("effect") == "dissolve", got_fx
    other_fx = json.loads(_exec_tool(doc, ctx, "get_slide_transition", {"page": 0}))
    assert other_fx.get("effect") != "dissolve", other_fx

    back = _exec_tool(doc, ctx, "move_slide", {"from_page": 1, "to_page": 0})
    assert json.loads(back).get("status") == "ok", back
    home = doc.getDrawPages().getByIndex(0)
    texts, groups = _impress_group_children(home)
    assert 2 in groups, groups
    home_notes = json.loads(_exec_tool(doc, ctx, "get_speaker_notes", {"page": 0}))
    assert "keep-these-notes" in (home_notes.get("notes") or ""), home_notes
    home_layout = json.loads(_exec_tool(doc, ctx, "get_slide_layout", {"page": 0}))
    assert home_layout.get("layout_name") == "title_only", home_layout
