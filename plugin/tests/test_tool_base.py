from plugin.framework.tool_base import ToolBase

class ValidTool(ToolBase):
    name = "edit_doc"
    description = "edit doc"
    parameters = {
        "properties": {
            "text": {"type": "string"}
        },
        "required": ["text"]
    }

    def execute(self, ctx, **kwargs):
        return {"status": "ok"}

class ReadTool(ToolBase):
    name = "get_info"

    def execute(self, ctx, **kwargs):
        pass

class ExplictMutateTool(ToolBase):
    name = "get_but_mutates"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        pass

def test_detects_mutation():
    tool1 = ValidTool()
    assert tool1.detects_mutation() is True  # does not start with get_

    tool2 = ReadTool()
    assert tool2.detects_mutation() is False # starts with get_

    tool3 = ExplictMutateTool()
    assert tool3.detects_mutation() is True  # is_mutation is explicit

    class UnnamedTool(ToolBase):
        name = None
        def execute(self, ctx, **kwargs): pass

    assert UnnamedTool().detects_mutation() is True

def test_validate():
    tool = ValidTool()

    # Valid
    ok, err = tool.validate(text="hello")
    assert ok is True
    assert err is None

    # Missing required
    ok, err = tool.validate()
    assert ok is False
    assert "Missing required parameter: text" in err

    # Unknown param
    ok, err = tool.validate(text="hello", extra="bad")
    assert ok is False
    assert "Unknown parameter: extra" in err

class MockDoc:
    def __init__(self, items=None):
        self._items = items or {}

    def getMyItems(self):
        class Collection:
            def __init__(self, data):
                self.data = data
            def hasByName(self, name):
                return name in self.data
            def getByName(self, name):
                return self.data[name]
            def getElementNames(self):
                return tuple(self.data.keys())
        return Collection(self._items)

def test_get_collection():
    tool = ValidTool()

    # Missing getter
    doc_bad = object()
    res = tool.get_collection(doc_bad, "getMyItems")
    assert isinstance(res, dict)
    assert res["status"] == "error"

    # Valid getter
    doc_good = MockDoc({"a": 1})
    coll = tool.get_collection(doc_good, "getMyItems")
    assert not isinstance(coll, dict)
    assert coll.hasByName("a")

def test_get_item():
    tool = ValidTool()
    doc = MockDoc({"item1": "val1", "item2": "val2"})

    # Missing getter entirely
    res = tool.get_item(object(), "getMyItems", "item1")
    assert isinstance(res, dict)
    assert res["status"] == "error"

    # Item not found
    res = tool.get_item(doc, "getMyItems", "missing")
    assert isinstance(res, dict)
    assert res["status"] == "error"
    assert "missing" in res["message"]
    assert "available" in res["details"]
    assert "item1" in res["details"]["available"]

    # Item found
    res = tool.get_item(doc, "getMyItems", "item1")
    assert res == "val1"

