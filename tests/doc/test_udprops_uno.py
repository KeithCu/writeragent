from plugin.doc.udprops import (
    get_document_property,
    list_user_defined_document_properties,
    read_builtin_document_properties,
    remove_document_property,
    set_document_property,
)
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("writer")
def test_udprop_set_twice_get_and_idempotent_remove(ctx, doc):
    name = "WriterAgentK085Probe"
    assert set_document_property(doc, name, "one") is True
    # Regression: the second save must update, not raise "Property name or handle already used".
    assert set_document_property(doc, name, "two") is True
    assert get_document_property(doc, name) == "two"
    assert remove_document_property(doc, name) is True
    assert get_document_property(doc, name, "default") == "default"
    assert remove_document_property(doc, name) is True


@native_test
@with_native_doc("writer")
def test_udprops_read_write_roundtrip_writer(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    from plugin.writer.specialized.fields import DocumentPropertiesGet, DocumentPropertiesSet

    set_res = DocumentPropertiesSet().execute(
        tool_ctx,
        title="Title from test",
        keywords=["alpha", "beta"],
        user_defined={"CustomProp": "value"},
    )
    assert set_res.get("status") == "ok", set_res

    got = DocumentPropertiesGet().execute(tool_ctx)
    assert got.get("title") == "Title from test"
    assert got.get("keywords") == ["alpha", "beta"]
    assert got.get("user_defined") == {"CustomProp": "value"}

    assert set_document_property(doc, "WriterAgentProbe", "hidden") is True
    got2 = DocumentPropertiesGet().execute(tool_ctx)
    assert "WriterAgentProbe" in got2.get("internal_omitted", [])
    assert "WriterAgentProbe" not in got2.get("user_defined", {})

    bad = DocumentPropertiesSet().execute(tool_ctx, user_defined={"WriterAgentNo": "x"})
    assert bad.get("status") == "error"

    assert remove_document_property(doc, "CustomProp") is True
    assert get_document_property(doc, "CustomProp", "gone") == "gone"
    assert "CustomProp" not in list_user_defined_document_properties(doc)


@native_test
@with_native_doc("calc")
def test_udprops_builtin_roundtrip_calc(ctx, doc):
    props = read_builtin_document_properties(doc)
    assert "title" in props
    assert set_document_property(doc, "CalcCustom", "1") is True
    assert get_document_property(doc, "CalcCustom") == "1"
    assert remove_document_property(doc, "CalcCustom") is True
