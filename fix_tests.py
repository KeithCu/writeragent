import re

with open("tests/calc/python/test_function.py", "r") as f:
    c = f.read()

# 1. Update test docs to have RuntimeUID corresponding to the calc: url so _lifecycle_key returns it
c = c.replace('doc = CalcDocStub(url="file:///fake_doc.ods")', 'doc = CalcDocStub(url="file:///fake_doc.ods", props={"RuntimeUID": "calc:file:///fake_doc.ods"})')
c = c.replace('doc = CalcDocStub(url="file:///fake.ods", selection="B2")', 'doc = CalcDocStub(url="file:///fake.ods", selection="B2", props={"RuntimeUID": "calc:file:///fake.ods"})')
c = c.replace('doc = CalcDocStub(url="file:///multi.ods", selection="A1")', 'doc = CalcDocStub(url="file:///multi.ods", selection="A1", props={"RuntimeUID": "calc:file:///multi.ods"})')
c = c.replace('doc = CalcDocStub(url="file:///fake2d.ods", selection="B2")', 'doc = CalcDocStub(url="file:///fake2d.ods", selection="B2", props={"RuntimeUID": "calc:file:///fake2d.ods"})')
c = c.replace('url="file:///SpillTest.ods",', 'url="file:///SpillTest.ods",\n            props={"RuntimeUID": "calc:file:///SpillTest.ods"},')
c = c.replace('doc = CalcDocStub(url="file:///offmain-spill.ods", selection="B2")', 'doc = CalcDocStub(url="file:///offmain-spill.ods", selection="B2", props={"RuntimeUID": "calc:file:///offmain-spill.ods"})')
c = c.replace('doc = CalcDocStub(url="file:///fake_cleanup.ods")', 'doc = CalcDocStub(url="file:///fake_cleanup.ods", props={"RuntimeUID": "calc:file:///fake_cleanup.ods"})')
c = c.replace('doc = CalcDocStub(url="file:///fake_pymt.ods")', 'doc = CalcDocStub(url="file:///fake_pymt.ods", props={"RuntimeUID": "calc:file:///fake_pymt.ods"})')

# 2. Replace keys
c = re.sub(r'key = \("file:///(.*?)\.ods"', r'key = ("calc:file:///\1.ods"', c)
c = re.sub(r'\("file:///(.*?)\.ods"', r'("calc:file:///\1.ods"', c)

# Fix where we might have accidentally replaced URL initialization:
c = c.replace('doc_url="calc:file:///', 'doc_url="file:///')
c = c.replace('url="calc:file:///', 'url="file:///')
c = c.replace('("calc:file:///fake_cleanup.ods", props', '("file:///fake_cleanup.ods", props')
c = c.replace('("calc:file:///owner.ods", props', '("file:///owner.ods", props')
c = c.replace('("calc:file:///active.ods", props', '("file:///active.ods", props')
c = c.replace('("calc:file:///fake_pymt.ods", props', '("file:///fake_pymt.ods", props')

# Fix Listener instantiations
c = c.replace('listener = python_function.CalcSpillModifyListener(ctx, "file:///fake_cleanup.ods", "Sheet1")', 'listener = python_function.CalcSpillModifyListener(ctx, "calc:file:///fake_cleanup.ods", "Sheet1")')
c = c.replace('listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///owner.ods", "Sheet1")', 'listener = python_function.CalcSpillModifyListener(MagicMock(), "uid-owner", "Sheet1")')
c = c.replace('listener = python_function.CalcSpillModifyListener(MagicMock(), "file:///fake_pymt.ods", "Sheet1")', 'listener = python_function.CalcSpillModifyListener(MagicMock(), "calc:file:///fake_pymt.ods", "Sheet1")')

# Replace key in orphan cleanup
c = c.replace('key = ("calc:file:///owner.ods", "Sheet1", 1, 1)', 'key = ("uid-owner", "Sheet1", 1, 1)')

# Add missing SPILL_REGISTRY.clear()s
c = re.sub(r'(?m)^([ \t]+)(doc = CalcDocStub\(url="file:///fake_cleanup\.ods", props=\{"RuntimeUID": "calc:file:///fake_cleanup\.ods"\}\))',
           r'\1\2\n\1python_function.SPILL_REGISTRY.clear()', c)

c = re.sub(r'(?m)^([ \t]+)(active = CalcDocStub\(url="file:///active\.ods", props=\{"RuntimeUID": "uid-active"\}\))',
           r'\1\2\n\1python_function.SPILL_REGISTRY.clear()', c)

c = re.sub(r'(?m)^([ \t]+)(doc = CalcDocStub\(url="file:///fake_pymt\.ods", props=\{"RuntimeUID": "calc:file:///fake_pymt\.ods"\}\))',
           r'\1\2\n\1python_function.SPILL_REGISTRY.clear()', c)

# Append the rename/delete tests
test_func = '''
def test_rename_spill_registry_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///rename.ods", props={"RuntimeUID": "calc:file:///rename.ods"})
    python_function.SPILL_REGISTRY.clear()
    key = ("calc:file:///rename.ods", "OldName", 1, 1)
    python_function.SPILL_REGISTRY[key] = [(2, 1)]

    saved = []
    monkeypatch.setattr(python_function, "save_spill_registry_for_doc", lambda d: saved.append(d))

    python_function.rename_spill_registry_sheet(doc, "OldName", "NewName")

    assert key not in python_function.SPILL_REGISTRY
    new_key = ("calc:file:///rename.ods", "NewName", 1, 1)
    assert python_function.SPILL_REGISTRY[new_key] == [(2, 1)]
    assert len(saved) == 1

def test_delete_spill_registry_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = CalcDocStub(url="file:///delete.ods", props={"RuntimeUID": "calc:file:///delete.ods"})
    python_function.SPILL_REGISTRY.clear()
    key1 = ("calc:file:///delete.ods", "Sheet1", 1, 1)
    key2 = ("calc:file:///delete.ods", "Sheet2", 1, 1)
    python_function.SPILL_REGISTRY[key1] = [(2, 1)]
    python_function.SPILL_REGISTRY[key2] = [(3, 1)]

    saved = []
    monkeypatch.setattr(python_function, "save_spill_registry_for_doc", lambda d: saved.append(d))

    python_function.delete_spill_registry_sheet(doc, "Sheet1")

    assert key1 not in python_function.SPILL_REGISTRY
    assert key2 in python_function.SPILL_REGISTRY
    assert len(saved) == 1

def test_save_as_does_not_orphan_spill_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Save-As changes doc url but not lifecycle id. Ensure spill registry is intact."""
    doc = CalcDocStub(url="file:///old.ods", props={"RuntimeUID": "calc:file:///old.ods"})
    python_function.SPILL_REGISTRY.clear()

    # Spill triggers before Save-As
    key = ("calc:file:///old.ods", "Sheet1", 1, 1)
    python_function.SPILL_REGISTRY[key] = [(2, 1)]

    # Save-As occurs, URL changes but RuntimeUID remains the same
    doc.url = "file:///new.ods"

    # A subsequent operation (e.g. rename) should correctly target the existing key because _lifecycle_key remains the same
    saved = []
    monkeypatch.setattr(python_function, "save_spill_registry_for_doc", lambda d: saved.append(d))

    python_function.rename_spill_registry_sheet(doc, "Sheet1", "NewName")

    assert key not in python_function.SPILL_REGISTRY
    new_key = ("calc:file:///old.ods", "NewName", 1, 1)
    assert python_function.SPILL_REGISTRY[new_key] == [(2, 1)]
    assert len(saved) == 1
'''
c += test_func

with open("tests/calc/python/test_function.py", "w") as f:
    f.write(c)
