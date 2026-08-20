# WriterAgent - Tests for FormulaLocationCache and formula locator

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.calc.python.formula_locator_cache import (
    FormulaLocationCache,
    document_cache_key,
    is_matching_py_formula,
    locate_formula_cell,
    locate_formula_cell_in_doc,
)
from tests.testing_utils import CalcDocStub, CalcSheetStub


def _ctx_with_doc(doc: CalcDocStub):
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    return SimpleNamespace(ServiceManager=smgr)


def test_cache_put_get_and_mru_promotion():
    cache = FormulaLocationCache(max_formulas_per_doc=5)
    cache.put("doc1", "code_a", "Sheet1", 0, 0)
    cache.put("doc1", "code_a", "Sheet2", 1, 1)

    coords = cache.get("doc1", "code_a")
    # MRU order: Sheet2!B2 should be first, Sheet1!A1 second
    assert coords == [("Sheet2", 1, 1), ("Sheet1", 0, 0)]

    # Promoting Sheet1!A1 moves it to front
    cache.put("doc1", "code_a", "Sheet1", 0, 0)
    assert cache.get("doc1", "code_a") == [("Sheet1", 0, 0), ("Sheet2", 1, 1)]


def test_cache_remove_coordinate_and_empty_cleanup():
    cache = FormulaLocationCache(max_formulas_per_doc=5)
    cache.put("doc1", "code_a", "Sheet1", 0, 0)
    cache.put("doc1", "code_a", "Sheet2", 1, 1)

    cache.remove_coordinate("doc1", "code_a", "Sheet1", 0, 0)
    assert cache.get("doc1", "code_a") == [("Sheet2", 1, 1)]

    cache.remove_coordinate("doc1", "code_a", "Sheet2", 1, 1)
    assert cache.get("doc1", "code_a") == []
    assert len(cache) == 0
    assert cache.document_count() == 0


def test_cache_lru_eviction_bounds_capacity_per_doc():
    cache = FormulaLocationCache(max_formulas_per_doc=3)
    cache.put("doc1", "code_1", "Sheet1", 0, 0)
    cache.put("doc1", "code_2", "Sheet1", 1, 0)
    cache.put("doc1", "code_3", "Sheet1", 2, 0)
    assert cache.formula_count("doc1") == 3

    # Access code_1 to make it MRU
    _ = cache.get("doc1", "code_1")

    # Insert code_4 -> should evict code_2 (the oldest)
    cache.put("doc1", "code_4", "Sheet1", 3, 0)
    assert cache.formula_count("doc1") == 3
    assert cache.get("doc1", "code_2") == []
    assert cache.get("doc1", "code_1") == [("Sheet1", 0, 0)]
    assert cache.get("doc1", "code_3") == [("Sheet1", 2, 0)]
    assert cache.get("doc1", "code_4") == [("Sheet1", 3, 0)]


def test_per_document_isolation_and_clear_document():
    """Formulas in doc1 and doc2 are completely isolated; clear_document cleans up only the target doc."""
    cache = FormulaLocationCache(max_formulas_per_doc=10)
    cache.put("file:///doc1.ods", "code_shared", "Sheet1", 0, 0)
    cache.put("file:///doc2.ods", "code_shared", "SheetA", 5, 5)
    cache.put("file:///doc1.ods", "code_unique1", "Sheet1", 1, 0)

    assert cache.document_count() == 2
    assert cache.formula_count("file:///doc1.ods") == 2
    assert cache.formula_count("file:///doc2.ods") == 1

    # Same code resolves to doc-specific coordinates
    assert cache.get("file:///doc1.ods", "code_shared") == [("Sheet1", 0, 0)]
    assert cache.get("file:///doc2.ods", "code_shared") == [("SheetA", 5, 5)]

    # Clear doc1 on unload
    cache.clear_document("file:///doc1.ods")
    assert cache.document_count() == 1
    assert cache.get("file:///doc1.ods", "code_shared") == []
    assert cache.get("file:///doc1.ods", "code_unique1") == []
    # doc2 remains intact
    assert cache.get("file:///doc2.ods", "code_shared") == [("SheetA", 5, 5)]


def test_unbounded_concurrent_documents():
    """FormulaLocationCache supports any number of concurrently open documents without arbitrary caps."""
    cache = FormulaLocationCache(max_formulas_per_doc=5)
    # Simulate 200 concurrently open workbooks (e.g. on a multi-session server)
    for i in range(200):
        cache.put(f"file:///server/doc_{i}.ods", f"code_{i}", "Sheet1", i % 10, i % 5)

    assert cache.document_count() == 200
    assert cache.get("file:///server/doc_0.ods", "code_0") == [("Sheet1", 0, 0)]
    assert cache.get("file:///server/doc_199.ods", "code_199") == [("Sheet1", 9, 4)]

    # Clean up 50 documents
    for i in range(50):
        cache.clear_document(f"file:///server/doc_{i}.ods")

    assert cache.document_count() == 150
    assert cache.get("file:///server/doc_0.ods", "code_0") == []
    assert cache.get("file:///server/doc_50.ods", "code_50") == [("Sheet1", 0, 0)]


def test_document_idle_ttl_expiration():
    """Documents idle longer than ttl_seconds are automatically purged on subsequent access."""
    import time

    cache = FormulaLocationCache(max_formulas_per_doc=5, ttl_seconds=0.05)
    cache.put("doc_expire_1", "code_1", "Sheet1", 0, 0)
    cache.put("doc_expire_2", "code_2", "Sheet1", 1, 1)
    assert cache.document_count() == 2

    # Wait for TTL to expire
    time.sleep(0.08)

    # Next access should trigger auto-pruning of idle documents
    assert cache.get("doc_expire_1", "code_1") == []
    assert cache.document_count() == 0


def test_is_matching_py_formula_variants():
    assert is_matching_py_formula('=PY("plt.plot([1, 2])")', "plt.plot([1, 2])")
    assert is_matching_py_formula('=PYTHON("x = ""hello""")', 'x = "hello"')
    assert is_matching_py_formula('=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("plt.figure(); plt.show()")', "plt.figure(); plt.show()")
    assert not is_matching_py_formula('=SUM(A1:A10)', "plt.plot()")
    assert not is_matching_py_formula('', "plt.plot()")
    assert not is_matching_py_formula('=PY("plt.plot([1, 2])")', "")
    # Shared 30-char prefix must not match a different script.
    code_a = "plt.figure(); plt.plot([1, 2, 3]); plt.title('Sales')"
    code_b = "plt.figure(); plt.plot([1, 2, 3]); plt.title('Costs')"
    assert code_a[:30] == code_b[:30]
    assert not is_matching_py_formula(f'=PY("{code_a}")', code_b)


def test_locate_formula_cell_in_doc_populates_cache_and_hits_on_second_call():
    cache = FormulaLocationCache(max_formulas_per_doc=10)
    sheet1 = CalcSheetStub("Overview")
    sheet2 = CalcSheetStub("Viz_Gallery")
    doc = CalcDocStub(sheets=[sheet1, sheet2], url="file:///test.ods", active_sheet="Overview")
    ctx = _ctx_with_doc(doc)

    code = "plt.plot([1, 2, 3])"
    cell = sheet2.getCellByPosition(3, 6)
    cell.setFormula(f'=PY("{code}")')

    # First call: full search
    located1 = locate_formula_cell_in_doc(ctx, doc, code, cache=cache)
    assert located1 is not None
    assert located1[0].getName() == "Viz_Gallery"
    assert located1[2] == (6, 3)
    assert cache.get(document_cache_key(doc), code) == [("Viz_Gallery", 6, 3)]

    # Mock sheet2.queryContentCells to ensure second call uses cache instead of scanning
    sheet2.queryContentCells = MagicMock(side_effect=AssertionError("Should not query content cells on cache hit"))
    sheet1.queryContentCells = MagicMock(side_effect=AssertionError("Should not query content cells on cache hit"))

    located2 = locate_formula_cell_in_doc(ctx, doc, code, cache=cache)
    assert located2 is not None
    assert located2[0].getName() == "Viz_Gallery"
    assert located2[2] == (6, 3)


def test_locate_formula_cell_in_doc_stale_cache_recovery():
    cache = FormulaLocationCache(max_formulas_per_doc=10)
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///test.ods")
    ctx = _ctx_with_doc(doc)

    code = "plt.hist([1, 2])"
    # Seed cache with stale coordinate (row 0, col 0)
    cache.put(document_cache_key(doc), code, "Sheet1", 0, 0)

    # Actual formula is at row 5, col 2
    cell = sheet.getCellByPosition(2, 5)
    cell.setFormula(f'=PY("{code}")')

    located = locate_formula_cell_in_doc(ctx, doc, code, cache=cache)
    assert located is not None
    assert located[2] == (5, 2)
    # Cache should now have the updated location (stale (0, 0) pruned)
    assert cache.get(document_cache_key(doc), code) == [("Sheet1", 5, 2)]


def test_locate_formula_cell_convenience_helper():
    sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[sheet], url="file:///test.ods")
    ctx = _ctx_with_doc(doc)

    code = "result = 99"
    sheet.getCellByPosition(1, 2).setFormula(f'=PYTHON("{code}")')

    coords = locate_formula_cell(ctx, sheet, code)
    assert coords == (2, 1)


def test_extract_code_from_py_formula():
    from plugin.calc.python.formula_locator_cache import extract_code_from_py_formula

    assert extract_code_from_py_formula('=PY("plt.plot([1, 2])")') == "plt.plot([1, 2])"
    assert extract_code_from_py_formula('=PYTHON("x = ""hello""", A1:B10)') == 'x = "hello"'
    assert (
        extract_code_from_py_formula(
            '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("import matplotlib.pyplot as plt\nplt.show()")'
        )
        == "import matplotlib.pyplot as plt\nplt.show()"
    )
    assert extract_code_from_py_formula("=SUM(A1:A10)") is None
    assert extract_code_from_py_formula("") is None


def test_opportunistic_batch_caching_warms_all_sheet_formulas():
    """Searching for one formula opportunistically indexes all other =PY() formulas on that sheet."""
    cache = FormulaLocationCache(max_formulas_per_doc=50)
    sheet = CalcSheetStub("Analytics")
    doc = CalcDocStub(sheets=[sheet], url="file:///batch.ods", selection="Z100")
    ctx = _ctx_with_doc(doc)

    code_1 = "plt.plot([1, 2])"
    code_2 = "plt.hist([10, 20])"
    code_3 = "result = data.mean()"

    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code_1}")')
    sheet.getCellByPosition(2, 5).setFormula(f'=PY("{code_2}")')
    sheet.getCellByPosition(4, 10).setFormula(f'=PYTHON("{code_3}", A1:A10)')

    # Search for code_1 only
    located_1 = locate_formula_cell_in_doc(ctx, doc, code_1, cache=cache)
    assert located_1 is not None
    assert located_1[2] == (0, 0)

    # Verify that code_2 and code_3 are already populated in the cache!
    assert cache.get(document_cache_key(doc), code_2) == [("Analytics", 5, 2)]
    assert cache.get(document_cache_key(doc), code_3) == [("Analytics", 10, 4)]

    # Now verify looking up code_2 and code_3 hits cache without querying content cells
    sheet.queryContentCells = MagicMock(side_effect=AssertionError("Should not query content cells; cache is warm!"))

    located_2 = locate_formula_cell_in_doc(ctx, doc, code_2, cache=cache)
    assert located_2 is not None
    assert located_2[2] == (5, 2)

    located_3 = locate_formula_cell_in_doc(ctx, doc, code_3, cache=cache)
    assert located_3 is not None
    assert located_3[2] == (10, 4)


def test_locate_formula_cell_in_doc_secondary_sheet_when_active_sheet_differs():
    """Formula cell on secondary sheet (viz) is located even when controller active sheet is analysis."""
    sheet_analysis = CalcSheetStub("analysis")
    sheet_viz = CalcSheetStub("viz")
    doc = CalcDocStub(sheets=[sheet_analysis, sheet_viz], url="file:///test_demo.ods", active_sheet="analysis")
    ctx = _ctx_with_doc(doc)

    code = "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.plot([3, 2, 1])"
    # Formula is on viz sheet at B2 (col=1, row=1)
    sheet_viz.getCellByPosition(1, 1).setFormula(
        f'=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON("{code}")'
    )

    located = locate_formula_cell_in_doc(ctx, doc, code)
    assert located is not None
    found_sheet, found_cell, (r, c) = located
    assert found_sheet.getName() == "viz"
    assert (r, c) == (1, 1)


def test_untitled_docs_do_not_share_formula_cache():
    """Empty getURL() must not collide; RuntimeUID (lifecycle key) isolates unsaved books."""
    cache = FormulaLocationCache(max_formulas_per_doc=10)
    code = "plt.plot([1])"
    doc1 = CalcDocStub(url="", props={"RuntimeUID": "uid-untitled-1"})
    doc2 = CalcDocStub(url="", props={"RuntimeUID": "uid-untitled-2"})
    ctx1 = _ctx_with_doc(doc1)
    ctx2 = _ctx_with_doc(doc2)

    doc1.getSheets().getByIndex(0).getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    doc2.getSheets().getByIndex(0).getCellByPosition(5, 5).setFormula(f'=PY("{code}")')

    located1 = locate_formula_cell_in_doc(ctx1, doc1, code, cache=cache)
    located2 = locate_formula_cell_in_doc(ctx2, doc2, code, cache=cache)
    assert located1 is not None and located1[2] == (0, 0)
    assert located2 is not None and located2[2] == (5, 5)

    key1 = document_cache_key(doc1)
    key2 = document_cache_key(doc2)
    assert key1 == "uid-untitled-1"
    assert key2 == "uid-untitled-2"
    assert cache.get(key1, code) == [("Sheet1", 0, 0)]
    assert cache.get(key2, code) == [("Sheet1", 5, 5)]

    cache.clear_document(key1)
    assert cache.get(key1, code) == []
    assert cache.get(key2, code) == [("Sheet1", 5, 5)]
