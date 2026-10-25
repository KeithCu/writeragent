from plugin.doc.udprops import get_document_property, remove_document_property, set_document_property
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


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
