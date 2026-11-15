from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.writer.specialized import sections
from plugin.writer.specialized.sections import (
    SectionCreate,
    SectionDelete,
    SectionSetColumns,
    SectionSetProtection,
)


def _mock_ctx(doc: MagicMock | None = None, doc_svc: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.doc = doc or MagicMock()
    ctx.services = SimpleNamespace(document=doc_svc or MagicMock())
    return ctx


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "S", "column_count": 0},
        {"name": "S", "column_count": 100},
        {"name": "S", "column_count": 2, "spacing_mm": -1},
        {"name": "S", "column_count": 2, "spacing_mm": float("inf")},
    ],
)
def test_section_set_columns_rejects_bad_values_before_touching_doc(kwargs: dict[str, object]) -> None:
    doc = MagicMock()
    res = SectionSetColumns().execute(_mock_ctx(doc=doc), **kwargs)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    doc.getTextSections.assert_not_called()


def test_section_create_rejects_end_before_start() -> None:
    doc = MagicMock()
    doc.getTextSections.return_value.hasByName.return_value = False
    doc_svc = MagicMock()
    doc_svc.resolve_locator.side_effect = [{"para_index": 3}, {"para_index": 1}]
    doc_svc.get_paragraph_ranges.return_value = [MagicMock() for _unused in range(5)]

    res = SectionCreate().execute(
        _mock_ctx(doc=doc, doc_svc=doc_svc),
        name="S",
        locator="paragraph:3",
        end_locator="paragraph:1",
    )
    assert res["status"] == "error"
    doc.createInstance.assert_not_called()


def test_section_create_rejects_table_endpoint() -> None:
    doc = MagicMock()
    doc.getTextSections.return_value.hasByName.return_value = False
    doc_svc = MagicMock()
    doc_svc.resolve_locator.side_effect = [{"para_index": 0}, {"para_index": 0}]
    table_elem = MagicMock()
    table_elem.supportsService.return_value = False
    doc_svc.get_paragraph_ranges.return_value = [table_elem]

    res = SectionCreate().execute(
        _mock_ctx(doc=doc, doc_svc=doc_svc),
        name="S",
        locator="paragraph:0",
    )
    assert res["status"] == "error"
    doc.createInstance.assert_not_called()


def test_section_create_selection_unavailable_is_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = MagicMock()
    doc.getTextSections.return_value.hasByName.return_value = False

    def _raise(*_args: object, **_kwargs: object) -> object:
        raise ValueError("no selection")

    monkeypatch.setattr(sections, "resolve_target_cursor", _raise)
    res = SectionCreate().execute(_mock_ctx(doc=doc), name="S", locator="selection")
    assert res["status"] == "error"
    doc.createInstance.assert_not_called()

    res_with_end = SectionCreate().execute(
        _mock_ctx(doc=doc),
        name="S",
        locator="selection",
        end_locator="paragraph:1",
    )
    assert res_with_end["status"] == "error"
    doc.createInstance.assert_not_called()


@pytest.mark.parametrize(
    "doc_index,prot_key,expected_code",
    [
        ("Table of Contents1", b"", "INDEX_SECTION"),
        (None, b"\x01" * 20, "SECTION_PASSWORD_PROTECTED"),
    ],
)
def test_section_tools_refuse_index_and_password_sections(
    doc_index: str | None,
    prot_key: bytes,
    expected_code: str,
) -> None:
    section = MagicMock()

    def _get_prop(prop_name: str) -> object:
        if prop_name == "DocumentIndex":
            return doc_index
        if prop_name == "ProtectionKey":
            return prot_key
        raise AssertionError(f"Unexpected property read: {prop_name}")

    section.getPropertyValue.side_effect = _get_prop

    doc = MagicMock()
    supplier = doc.getTextSections.return_value
    supplier.hasByName.return_value = True
    supplier.getByName.return_value = section

    ctx = _mock_ctx(doc=doc)
    calls = [
        SectionDelete().execute(ctx, name="S"),
        SectionSetColumns().execute(ctx, name="S", column_count=2),
        SectionSetProtection().execute(ctx, name="S", protected=True),
    ]
    for res in calls:
        assert res["status"] == "error"
        assert res["code"] == expected_code

    section.setPropertyValue.assert_not_called()
