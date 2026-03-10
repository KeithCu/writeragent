import pytest
from plugin.framework.service_base import ServiceBase
from plugin.framework.service_registry import ServiceRegistry
from plugin.framework.tool_base import ToolBase
from plugin.framework.tool_registry import ToolRegistry

# --- ServiceRegistry Tests ---

class DummyService(ServiceBase):
    name = "dummy"
    def __init__(self):
        super().__init__()
        self.initialized = False
        self.shutdown_called = False

    def initialize(self, ctx):
        self.initialized = True
        self.ctx = ctx

    def shutdown(self):
        self.shutdown_called = True

def test_service_registry_register_and_get():
    registry = ServiceRegistry()
    svc = DummyService()

    registry.register(svc)
    assert registry.get("dummy") is svc
    assert registry.dummy is svc
    assert "dummy" in registry
    assert "dummy" in registry.service_names

def test_service_registry_register_instance():
    registry = ServiceRegistry()
    class RawInstance:
        pass
    inst = RawInstance()

    registry.register_instance("raw", inst)
    assert registry.get("raw") is inst
    assert registry.raw is inst

def test_service_registry_invalid_registration():
    registry = ServiceRegistry()

    class NamelessService(ServiceBase):
        name = None

    with pytest.raises(ValueError, match="has no name"):
        registry.register(NamelessService())

    svc = DummyService()
    registry.register(svc)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(DummyService())

    with pytest.raises(ValueError, match="already registered"):
        registry.register_instance("dummy", object())

def test_service_registry_getattr_missing():
    registry = ServiceRegistry()
    with pytest.raises(AttributeError, match="No service registered"):
        _ = registry.missing_service

    with pytest.raises(AttributeError):
        _ = registry._private_attr

def test_service_registry_lifecycle():
    registry = ServiceRegistry()
    svc = DummyService()
    registry.register(svc)

    ctx = {"fake": "context"}
    registry.initialize_all(ctx)
    assert svc.initialized
    assert svc.ctx is ctx

    registry.shutdown_all()
    assert svc.shutdown_called

def test_service_registry_shutdown_error_swallowed():
    registry = ServiceRegistry()
    class BadService(DummyService):
        name = "bad"
        def shutdown(self):
            raise RuntimeError("fail")

    svc = BadService()
    registry.register(svc)

    # Should not raise exception
    registry.shutdown_all()

# --- ToolRegistry Tests ---

class DummyTool(ToolBase):
    name = "dummy_tool"
    description = "A dummy tool"
    tier = "core"
    intent = "test"
    doc_types = ["writer"]
    parameters = {"properties": {}}

    def __init__(self):
        super().__init__()
        self.executed = False

    def execute(self, ctx, **kwargs):
        self.executed = True
        self.kwargs = kwargs
        return {"status": "success"}

class DummyContext:
    def __init__(self, doc_type="writer"):
        self.doc_type = doc_type
        self.caller = "test"
        self.doc = object()

class MockEventBus:
    def __init__(self):
        self.events = []
    def emit(self, event, **kwargs):
        self.events.append((event, kwargs))

class MockDocumentService:
    def __init__(self):
        self.invalidated = []
    def invalidate_cache(self, doc):
        self.invalidated.append(doc)

def test_tool_registry_register_and_get():
    services = ServiceRegistry()
    registry = ToolRegistry(services)
    tool = DummyTool()

    registry.register(tool)
    assert registry.get("dummy_tool") is tool
    assert "dummy_tool" in registry.tool_names
    assert len(registry) == 1

    # re-registering same type is silent
    registry.register(DummyTool())

def test_tool_registry_tools_for_doc_type():
    services = ServiceRegistry()
    registry = ToolRegistry(services)

    tool1 = DummyTool()
    tool1.name = "t1"
    tool1.doc_types = ["writer"]

    tool2 = DummyTool()
    tool2.name = "t2"
    tool2.doc_types = ["calc"]

    tool3 = DummyTool()
    tool3.name = "t3"
    tool3.doc_types = None

    registry.register_many([tool1, tool2, tool3])

    writer_tools = list(registry.tools_for_doc_type("writer"))
    assert tool1 in writer_tools
    assert tool3 in writer_tools
    assert tool2 not in writer_tools

    calc_tools = list(registry.tools_for_doc_type("calc"))
    assert tool2 in calc_tools
    assert tool3 in calc_tools
    assert tool1 not in calc_tools

def test_tool_registry_schemas():
    services = ServiceRegistry()
    registry = ToolRegistry(services)

    tool = DummyTool()
    registry.register(tool)

    # Needs doc_type matching or doc_type=None
    schemas = registry.get_openai_schemas(tier="core", doc_type="writer")
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "dummy_tool"

    schemas = registry.get_openai_schemas_by_names(["dummy_tool", "missing"])
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "dummy_tool"

    summaries = registry.get_tool_summaries(tier="core", doc_type="writer")
    assert len(summaries) == 1
    assert summaries[0]["name"] == "dummy_tool"

    names = registry.get_tool_names_by_intent(doc_type="writer", intent="test")
    assert len(names) == 0  # tier is "core", not "extended"

    tool.tier = "extended"
    names = registry.get_tool_names_by_intent(doc_type="writer", intent="test")
    assert len(names) == 1

    mcp_schemas = registry.get_mcp_schemas(doc_type="writer")
    assert len(mcp_schemas) == 1
    assert mcp_schemas[0]["name"] == "dummy_tool"

def test_tool_registry_execute():
    services = ServiceRegistry()
    events = MockEventBus()
    doc_svc = MockDocumentService()
    services.register_instance("events", events)
    services.register_instance("document", doc_svc)

    registry = ToolRegistry(services)
    tool = DummyTool()
    tool.parameters = {"properties": {"arg1": {"type": "string"}}}
    registry.register(tool)

    ctx = DummyContext("writer")

    result = registry.execute("dummy_tool", ctx, arg1="val1", extra="ignored")

    assert result == {"status": "success"}
    assert tool.executed
    assert tool.kwargs == {"arg1": "val1"}  # extra was filtered out

    # Check events
    assert len(events.events) == 2
    assert events.events[0][0] == "tool:executing"
    assert events.events[1][0] == "tool:completed"

    # Check doc invalidation
    assert len(doc_svc.invalidated) == 1
    assert doc_svc.invalidated[0] is ctx.doc

def test_tool_registry_execute_errors():
    services = ServiceRegistry()
    events = MockEventBus()
    services.register_instance("events", events)
    registry = ToolRegistry(services)
    tool = DummyTool()
    registry.register(tool)

    ctx = DummyContext("writer")

    with pytest.raises(KeyError):
        registry.execute("missing", ctx)

    ctx_calc = DummyContext("calc")
    with pytest.raises(ValueError, match="does not support doc_type"):
        registry.execute("dummy_tool", ctx_calc)

    # Validation error
    class InvalidTool(DummyTool):
        name = "invalid"
        def validate(self, **kwargs):
            return False, "bad args"

    registry.register(InvalidTool())
    res = registry.execute("invalid", ctx)
    assert res == {"status": "error", "error": "bad args"}

    # Execution exception
    class CrashTool(DummyTool):
        name = "crash"
        def execute(self, ctx, **kwargs):
            raise RuntimeError("boom")

    registry.register(CrashTool())
    res = registry.execute("crash", ctx)
    assert res == {"status": "error", "error": "boom"}
    assert events.events[-1][0] == "tool:failed"

def test_tool_registry_discover(tmpdir):
    import sys
    services = ServiceRegistry()
    registry = ToolRegistry(services)

    # Create a fake tool module
    pkg_dir = tmpdir.mkdir("fake_tools")
    init_file = pkg_dir.join("__init__.py")
    init_file.write("")

    tool_file = pkg_dir.join("my_tool.py")
    tool_file.write("""
from plugin.framework.tool_base import ToolBase
class MyFakeTool(ToolBase):
    name = "my_fake"
    def execute(self, ctx, **kwargs): pass
""")

    sys.path.insert(0, str(tmpdir))
    try:
        registry.discover(str(pkg_dir), "fake_tools")
        assert "my_fake" in registry.tool_names
    finally:
        sys.path.pop(0)
