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
from plugin.tests.testing_utils import with_native_doc


def _exec_tool(doc, ctx, name, args):
    from plugin.app import get_tools
    from plugin.framework.tool import ToolContext

    active_idx = _active_draw_page_index(doc)
    tctx = ToolContext(doc, ctx, "draw", get_tools().services, "test", active_page_index=active_idx)
    res = get_tools().execute(name, tctx, **args)
    return json.dumps(res) if isinstance(res, dict) else res


def _active_draw_page_index(doc):
    """Index of the page the Draw controller treats as current (0 if unknown)."""
    if doc is None:
        return 0
    pages = doc.getDrawPages()
    ctrl = doc.getCurrentController()
    if ctrl is None or not hasattr(ctrl, "getCurrentPage"):
        return 0
    page = ctrl.getCurrentPage()
    if page is None:
        return 0
    for i in range(pages.getCount()):
        if pages.getByIndex(i) == page:
            return i
    return 0


@native_test
@with_native_doc("draw")
def test_list_pages(ctx, doc):
    result = _exec_tool(doc, ctx, "slide_list", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"slide_list failed: {result}"
    num_pages = data.get("count", len(data.get("pages", [])))
    assert num_pages > 0, "No pages found"


@native_test
@with_native_doc("draw")
def test_upsert_and_verify_shape(ctx, doc):
    # 0. Test slide_add
    initial_page_count = doc.getDrawPages().getCount()
    result = _exec_tool(doc, ctx, "slide_add", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"slide_add failed: {result}"
    new_page_count = doc.getDrawPages().getCount()
    assert new_page_count == initial_page_count + 1, "Page count did not increase after slide_add"
    inserted = doc.getDrawPages().getByIndex(new_page_count - 1)
    cur = doc.getCurrentController().getCurrentPage()
    assert cur is not None and cur == inserted, "slide_add should activate the new slide for subsequent tools"

    # 1. Create shape
    active_page = doc.getCurrentController().getCurrentPage()
    if active_page is None:
        active_page = doc.getDrawPages().getByIndex(0)
    initial_shape_count = active_page.getCount()

    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "x": 1000, "y": 1000, "width": 5000, "height": 3000,
        "text": "Hello Draw",
        "fill_color": "#FF0000"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_upsert create failed: {result}"

    new_shape_count = active_page.getCount()
    assert new_shape_count == initial_shape_count + 1, "Shape count did not increase after shape_upsert"

    # Query the created shape's Position and Size properties via UNO
    created_shape = active_page.getByIndex(new_shape_count - 1)
    pos = created_shape.getPosition()
    size = created_shape.getSize()
    assert pos.X == 1000, f"Expected X=1000, got {pos.X}"
    assert pos.Y == 1000, f"Expected Y=1000, got {pos.Y}"
    assert size.Width == 5000, f"Expected Width=5000, got {size.Width}"
    assert size.Height == 3000, f"Expected Height=3000, got {size.Height}"

    # 2. Get draw summary to find shape_id
    result = _exec_tool(doc, ctx, "shape_get_summary", {"page": new_page_count - 1})
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_get_summary failed: {result}"
    shapes = data.get("shapes", [])

    shape_id = None
    for s in shapes:
        if s.get("custom_shape_type") == "rectangle":
            shape_id = s.get("index")
    assert shape_id is not None, "Summary missing the created rectangle"

    # 3. Edit shape
    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "edit",
        "index": shape_id,
        "x": 3000, "y": 3000,
        "fill_color": "#00FF00"
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_upsert edit failed: {result}"

    # 4. Delete shape
    result = _exec_tool(doc, ctx, "shape_delete", {"index": shape_id})
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_delete failed: {result}"


@native_test
@with_native_doc("draw")
def test_create_custom_shape_octagon(ctx, doc):
    """Enhanced CustomShape types need CustomShapeEngine + geometry Type (e.g. octagon)."""
    active_page = doc.getCurrentController().getCurrentPage()
    if active_page is None:
        active_page = doc.getDrawPages().getByIndex(0)
    initial_shape_count = active_page.getCount()

    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "octagon",
        "x": 1000,
        "y": 1000,
        "width": 5000,
        "height": 5000,
        "fill_color": "none",
        "line_color": "black",
        "line_width": 100,
    })
    data = json.loads(result)
    assert data.get("status") == "ok", f"shape_upsert octagon failed: {result}"
    assert data.get("geometry_applied") is True, data
    assert "warning" not in data, data

    assert active_page.getCount() == initial_shape_count + 1, "Octagon not added to page"

    created = active_page.getByIndex(active_page.getCount() - 1)
    pos = created.getPosition()
    size = created.getSize()
    assert pos.X == 1000 and pos.Y == 1000, (pos.X, pos.Y)
    assert size.Width == 5000 and size.Height == 5000, (size.Width, size.Height)
    assert size.Width > 0 and size.Height > 0, "Shape must have non-zero size"


@native_test
@with_native_doc("draw")
def test_get_draw_context_for_chat(ctx, doc):
    from plugin.draw.bridge import get_draw_context_for_chat
    ctx_str = get_draw_context_for_chat(doc, 8000, ctx)
    has_doc_type = "Draw Document" in ctx_str or "Impress Presentation" in ctx_str
    has_total = "Total" in ctx_str and ("Pages" in ctx_str or "Slides" in ctx_str)
    assert has_doc_type and has_total, "get_draw_context_for_chat missing expected headers"


@native_test
@with_native_doc("draw")
def test_duplicate_slide_copies_shapes(ctx, doc):
    page0 = doc.getDrawPages().getByIndex(0)
    before = page0.getCount()
    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "page": 0,
        "x": 1000, "y": 1000, "width": 2000, "height": 1500,
        "text": "dup-me",
        "fill_color": "#FF0000",
    })
    data = json.loads(result)
    assert data.get("status") == "ok", result
    src_count = page0.getCount()
    assert src_count == before + 1

    result = _exec_tool(doc, ctx, "slide_duplicate", {"page": 0, "activate": False})
    data = json.loads(result)
    assert data.get("status") == "ok", result
    copy = doc.getDrawPages().getByIndex(1)
    assert copy.getCount() == src_count
    texts = []
    for i in range(copy.getCount()):
        shape = copy.getByIndex(i)
        try:
            texts.append(shape.getString())
        except Exception:
            pass
    assert "dup-me" in texts


@native_test
@with_native_doc("draw")
def test_duplicate_rename_move_slide(ctx, doc):
    initial = doc.getDrawPages().getCount()
    created = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "page": 0,
        "x": 1000, "y": 1000, "width": 2000, "height": 1500,
        "text": "move-me",
    })
    assert json.loads(created).get("status") == "ok", created

    result = _exec_tool(doc, ctx, "slide_duplicate", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", result
    assert doc.getDrawPages().getCount() == initial + 1

    result = _exec_tool(doc, ctx, "slide_rename", {"page": 1, "name": "Copy"})
    data = json.loads(result)
    assert data.get("status") == "ok", result
    page = doc.getDrawPages().getByIndex(1)
    if hasattr(page, "Name"):
        assert page.Name == "Copy"

    result = _exec_tool(doc, ctx, "slide_move", {"from_page": 1, "to_page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", result
    moved = doc.getDrawPages().getByIndex(0)
    if hasattr(moved, "Name"):
        assert moved.Name == "Copy"
    assert doc.getDrawPages().getCount() == initial + 1
    texts = []
    for i in range(moved.getCount()):
        shape = moved.getByIndex(i)
        try:
            texts.append(shape.getString())
        except Exception:
            pass
    assert "move-me" in texts


def _page_shape_marks(page):
    texts = []
    group_children = []
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        try:
            shape_type = shape.getShapeType()
        except Exception:
            shape_type = ""
        try:
            texts.append(shape.getString())
        except Exception:
            pass
        if "GroupShape" in shape_type:
            group_children.append(shape.getCount())
    return texts, group_children


@native_test
@with_native_doc("draw")
def test_move_slide_keeps_group(ctx, doc):
    """A grouped slide still has its children after a move, including back to index 0."""
    for label, x in (("g-left", 1000), ("g-right", 4000)):
        created = _exec_tool(doc, ctx, "shape_upsert", {
            "action": "create",
            "shape_type": "rectangle",
            "page": 0,
            "x": x, "y": 1000, "width": 2000, "height": 1500,
            "text": label,
        })
        assert json.loads(created).get("status") == "ok", created
    grouped = _exec_tool(doc, ctx, "shape_group", {"page": 0, "indices": [0, 1]})
    assert json.loads(grouped).get("status") == "ok", grouped
    labeled = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "page": 0,
        "x": 1000, "y": 4000, "width": 2000, "height": 800,
        "text": "move-me",
    })
    assert json.loads(labeled).get("status") == "ok", labeled
    renamed = _exec_tool(doc, ctx, "slide_rename", {"page": 0, "name": "Copy"})
    assert json.loads(renamed).get("status") == "ok", renamed
    added = _exec_tool(doc, ctx, "slide_add", {})
    assert json.loads(added).get("status") == "ok", added
    assert doc.getDrawPages().getCount() == 2

    moved = _exec_tool(doc, ctx, "slide_move", {"from_page": 0, "to_page": 1})
    assert json.loads(moved).get("status") == "ok", moved
    page = doc.getDrawPages().getByIndex(1)
    texts, groups = _page_shape_marks(page)
    assert "move-me" in texts, texts
    assert 2 in groups, groups
    if hasattr(page, "Name"):
        assert page.Name == "Copy"

    back = _exec_tool(doc, ctx, "slide_move", {"from_page": 1, "to_page": 0})
    assert json.loads(back).get("status") == "ok", back
    page = doc.getDrawPages().getByIndex(0)
    texts, groups = _page_shape_marks(page)
    assert "move-me" in texts, texts
    assert 2 in groups, groups
    if hasattr(page, "Name"):
        assert page.Name == "Copy"
    assert doc.getDrawPages().getCount() == 2


@native_test
@with_native_doc("draw")
def test_align_and_insert_table(ctx, doc):
    page = doc.getCurrentController().getCurrentPage()
    if page is None:
        page = doc.getDrawPages().getByIndex(0)
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create", "shape_type": "rectangle",
        "x": 1000, "y": 2000, "width": 2000, "height": 1000, "text": "L",
    })
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create", "shape_type": "rectangle",
        "x": 5000, "y": 4000, "width": 2000, "height": 1000, "text": "R",
    })
    n = page.getCount()
    result = _exec_tool(doc, ctx, "shape_align", {
        "indices": [n - 2, n - 1], "alignment": "top",
    })
    data = json.loads(result)
    assert data.get("status") == "ok", result
    a = page.getByIndex(n - 2).getPosition()
    b = page.getByIndex(n - 1).getPosition()
    assert a.Y == b.Y == 2000

    result = _exec_tool(doc, ctx, "table_insert", {
        "rows": 2, "columns": 2, "data": [["A", "B"], ["C", "D"]],
    })
    data = json.loads(result)
    assert data.get("status") == "ok", result
    assert data.get("cells_written") == 4, result


@native_test
@with_native_doc("draw")
def test_master_slides(ctx, doc):
    # 1. List master slides
    result = _exec_tool(doc, ctx, "slide_list_masters", {})
    data = json.loads(result)
    assert data.get("status") == "ok", f"slide_list_masters failed: {result}"
    master_slides = data.get("master_slides", [])
    assert len(master_slides) > 0, "No master slides found"

    first_master_name = master_slides[0].get("name")
    assert first_master_name is not None, "Master slide name is missing"

    # 2. Get slide master for page 0
    result = _exec_tool(doc, ctx, "slide_get_master", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"slide_get_master failed: {result}"

    # 3. Set slide master for page 0 to the first master we found
    result = _exec_tool(doc, ctx, "slide_set_master", {"page": 0, "master": first_master_name})
    data = json.loads(result)
    assert data.get("status") == "ok", f"slide_set_master failed: {result}"

    # 4. Verify it was set
    result = _exec_tool(doc, ctx, "slide_get_master", {"page": 0})
    data = json.loads(result)
    assert data.get("status") == "ok", f"slide_get_master verify failed: {result}"
    assert data.get("master") == first_master_name, f"Master name mismatch: {data.get('master')}"


@native_test
@with_native_doc("draw")
def test_get_draw_tree(ctx, doc):
    from plugin.testing_runner import _progress_verbose as _progress

    # GHA 34606276107: this TEST end OK; the next insert_math_draw close
    # killed soffice. Name the tree call vs teardown.
    _progress("get_tree: body start")
    # Ensure there is at least one shape to build a tree with
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "x": 1000, "y": 1000, "width": 5000, "height": 3000,
        "text": "Tree Shape",
        "fill_color": "#FF0000"
    })

    result = _exec_tool(doc, ctx, "shape_get_tree", {"page": _active_draw_page_index(doc)})
    data = json.loads(result)
    _progress("get_tree: execute done status=%s" % data.get("status"))
    assert data.get("status") == "ok", f"get_tree failed: {result}"
    tree = data.get("tree", [])
    assert len(tree) > 0, "Draw tree is empty"

    # Check if the shape we just created is in the tree
    found = False
    for node in tree:
        if node.get("text") == "Tree Shape":
            found = True
            break
    assert found, "Created shape not found in draw tree"
    _progress("get_tree: body done")


@native_test
@with_native_doc("draw")
def test_get_draw_tree_marks_blank_and_label_hint(ctx, doc):
    page_idx = _active_draw_page_index(doc)
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "text",
        "x": 500, "y": 2000, "width": 3500, "height": 800,
        "text": "Product:",
        "name": "lbl_product",
        "page": page_idx,
    })
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "text",
        "x": 4500, "y": 2000, "width": 5000, "height": 800,
        "text": "",
        "name": "fld_product",
        "page": page_idx,
    })

    result = _exec_tool(doc, ctx, "shape_get_tree", {"page": page_idx})
    data = json.loads(result)
    assert data.get("status") == "ok", f"get_tree failed: {result}"
    blanks = [n for n in data.get("tree", []) if n.get("fillable")]
    assert blanks, f"expected a fillable blank in tree: {data.get('tree')}"
    named = [n for n in blanks if n.get("name") == "fld_product"]
    assert named, f"blank named fld_product missing: {blanks}"
    assert named[0].get("label_hint") == "Product:"
    assert "geometry" in named[0]


@native_test
@with_native_doc("draw")
def test_shape_upsert_edit_by_name(ctx, doc):
    page_idx = _active_draw_page_index(doc)
    create = json.loads(_exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "text",
        "x": 1000, "y": 1000, "width": 4000, "height": 800,
        "text": "",
        "name": "fld_stable",
        "page": page_idx,
    }))
    assert create.get("status") == "ok", f"create failed: {create}"

    # A later shape would shift a naive "last index" fill; Name stays stable.
    _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "rectangle",
        "x": 100, "y": 100, "width": 500, "height": 500,
        "page": page_idx,
    })

    edit = json.loads(_exec_tool(doc, ctx, "shape_upsert", {
        "action": "edit",
        "name": "fld_stable",
        "text": "filled-by-name",
        "page": page_idx,
    }))
    assert edit.get("status") == "ok", f"edit by name failed: {edit}"

    page = doc.getCurrentController().getCurrentPage()
    found = None
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if getattr(shape, "Name", "") == "fld_stable":
            found = shape
            break
    assert found is not None, "named shape missing after edit"
    assert found.getString() == "filled-by-name"


@native_test
@with_native_doc("writer")
def test_writer_star24_with_text_keeps_explicit_size(ctx, doc):
    """Writer AT_PAGE custom shapes must not collapse after setString (Arch Universal Sample)."""
    page = doc.getDrawPages().getByIndex(0)
    before = page.getCount()
    result = _exec_tool(doc, ctx, "shape_upsert", {
        "action": "create",
        "shape_type": "star24",
        "x": 2000,
        "y": 5000,
        "width": 4000,
        "height": 4000,
        "fill_color": "blue",
        "text": "24-sided Star",
    })
    data = json.loads(result)
    assert data.get("status") == "ok", result
    assert data.get("geometry_applied") is True, data
    assert page.getCount() == before + 1
    shape = page.getByIndex(page.getCount() - 1)
    size = shape.getSize()
    # Allow 1 HMM rounding; reject Arch-style collapse to ~2249x489
    assert abs(size.Width - 4000) <= 2, f"Width collapsed: {size.Width}x{size.Height}"
    assert abs(size.Height - 4000) <= 2, f"Height collapsed: {size.Width}x{size.Height}"
    assert shape.String == "24-sided Star" or shape.getString() == "24-sided Star"


@native_test
@with_native_doc("impress", reuse=False)
def test_list_pages_reports_lo_default_names(ctx, doc):
    from plugin.draw.pages import AddSlide, ListPages
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    for _idx in range(2):
        added = AddSlide().execute(tctx)
        assert added.get("status") == "ok", added

    out = ListPages().execute(tctx)
    assert out.get("status") == "ok", out
    assert out["pages"] == [
        {"index": 0, "name": "page1", "title": "", "default_name": True},
        {"index": 1, "name": "page2", "title": "", "default_name": True},
        {"index": 2, "name": "page3", "title": "", "default_name": True},
    ]


@native_test
@with_native_doc("impress", reuse=False)
def test_list_pages_reports_slide_title(ctx, doc):
    from plugin.draw.pages import AddSlide, ListPages
    from plugin.draw.placeholders import SetPlaceholderText
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    assert AddSlide().execute(tctx, layout="text").get("status") == "ok"
    assert SetPlaceholderText().execute(tctx, role="title", text="Pricing", page=1).get("status") == "ok"
    assert AddSlide().execute(tctx, layout="blank").get("status") == "ok"

    out = ListPages().execute(tctx)
    assert out.get("status") == "ok", out
    assert out["pages"][1]["title"] == "Pricing"
    assert out["pages"][2]["title"] == ""


@native_test
@with_native_doc("impress", reuse=False)
def test_move_slide_reports_final_order(ctx, doc):
    from plugin.draw.pages import AddSlide, MoveSlide, RenameSlide
    from plugin.draw.shapes import UpsertShape
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    for _idx in range(2):
        assert AddSlide().execute(tctx).get("status") == "ok"

    for idx, title in enumerate(("Intro", "Body", "End")):
        assert RenameSlide().execute(tctx, page=idx, name=title).get("status") == "ok"

    assert (
        UpsertShape()
        .execute(
            tctx,
            action="create",
            page=0,
            shape_type="text",
            x=1000,
            y=1000,
            width=4000,
            height=1500,
            text="Intro slide body",
        )
        .get("status")
        == "ok"
    )

    out = MoveSlide().execute(tctx, from_page=0, to_page=2)
    assert out.get("status") == "ok", out
    assert out["to_page"] == 2
    assert [p["name"] for p in out["pages"]] == ["Body", "End", "Intro"]

    pages = doc.getDrawPages()
    assert [pages.getByIndex(i).Name for i in range(pages.getCount())] == ["Body", "End", "Intro"]
    # Bind every shape proxy once before inspecting it; a late release of a dropped proxy detaches the refetched one (docs/framework/uno-test-lifecycle.md).
    moved_page = pages.getByIndex(2)
    shapes = [moved_page.getByIndex(i) for i in range(moved_page.getCount())]
    texts = [s.getString() for s in shapes if hasattr(s, "getString")]
    assert "Intro slide body" in texts


@native_test
@with_native_doc("draw")
def test_insert_math_draw(ctx, doc):
    # Last Draw close in this file on Windows: close_doc of a Math OLE Draw
    # killed soffice (GHA 34607010446, exit 0). Run after the other tests
    # so a skipped leftover is not desktop current for later cases here.
    # The runner also defers this file until just before the peer suite
    # so this leftover is not closed (34607010446). Notebook detect hang
    # on 34619751330 was leftover Hidden _blank, not this Draw.
    from plugin.testing_runner import _progress_verbose as _progress
    from plugin.tests.testing_utils import mark_windows_math_ole_doc

    # GHA 34606276107: load done uid=48 then ~8s later close_doc dispose
    # and soffice exited 0. Name math_insert vs close_doc teardown.
    _progress("insert_math_draw: math_insert start")
    result = _exec_tool(doc, ctx, "math_insert", {
        "formula_type": "latex",
        "formula": "E = mc^2",
        "page": 0,
        "x": 2000,
        "y": 2000,
    })

    data = json.loads(result)
    _progress("insert_math_draw: math_insert done status=%s" % data.get("status"))
    assert data.get("status") == "ok", f"math_insert failed: {result}"

    # math_insert used page_index 0 — read shape from that page (current slide may differ after prior tests).
    target_page = doc.getDrawPages().getByIndex(0)
    shape = target_page.getByIndex(data.get("index"))

    assert shape.CLSID == "078B7ABA-54FC-457F-8551-6147e776a997"
    sz = shape.getSize()
    assert sz.Width >= 400 and sz.Height >= 300, f"expected plausible size, got {sz.Width}x{sz.Height}"

    # Drop Math OLE proxies before teardown. close_doc still GCs; Windows
    # skips the close after mark (34607010446 dispose killed soffice).
    mark_windows_math_ole_doc(doc)
    shape = None
    target_page = None
    _progress("insert_math_draw: body done")


@native_test
@with_native_doc("impress", reuse=False)
def test_rename_slide_refuses_reserved_and_duplicate_names_uno(ctx, doc):
    from plugin.draw.pages import AddSlide, ListPages, RenameSlide
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="impress", services=None, caller="test")
    for _idx in range(2):
        assert AddSlide().execute(tctx).get("status") == "ok"

    assert RenameSlide().execute(tctx, page=1, name="Intro").get("status") == "ok"

    reserved = RenameSlide().execute(tctx, page=1, name="Slide 9")
    assert reserved.get("status") == "error", reserved
    assert reserved.get("code") == "INVALID_PARAM", reserved

    listed = ListPages().execute(tctx)
    assert listed.get("status") == "ok", listed
    assert listed["pages"][1]["name"] == "Intro"

    dup_custom = RenameSlide().execute(tctx, page=0, name="Intro")
    assert dup_custom.get("status") == "error", dup_custom
    assert dup_custom.get("code") == "INVALID_PARAM", dup_custom

    listed_after = ListPages().execute(tctx)
    assert listed_after["pages"][0].get("default_name") is True

    dup_default = RenameSlide().execute(tctx, page=0, name="page3")
    assert dup_default.get("status") == "error", dup_default
    assert dup_default.get("code") == "INVALID_PARAM", dup_default
