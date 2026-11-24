from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.framework.service import ServiceRegistry
from plugin.framework.tool import ToolRegistry
from plugin.writer.specialized import sections
from plugin.writer.specialized.sections import (
    SectionCreate,
    SectionDelete,
    SectionGetInfo,
    SectionList,
    SectionRead,
    SectionRename,
    SectionSetColumns,
    SectionSetMargins,
    SectionSetPassword,
    SectionSetProtection,
    SectionSetVisibility,
    _hash_password,
    _password_matches,
)
from plugin.writer.specialized_base import SpecializedWorkflowFinished
from plugin.writer.structural import NavGotoPage


def _mock_ctx(doc: MagicMock | None = None, doc_svc: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.doc = doc or MagicMock()
    ctx.services = SimpleNamespace(document=doc_svc or MagicMock())
    return ctx


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "S", "column_count": 0, "spacing_mm": 0},
        {"name": "S", "column_count": 100, "spacing_mm": 0},
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
        SectionSetColumns().execute(ctx, name="S", column_count=2, spacing_mm=0),
        SectionSetProtection().execute(ctx, name="S", protected=True),
        SectionSetVisibility().execute(ctx, name="S", visible=False),
        SectionSetPassword().execute(ctx, name="S", password="x"),
        SectionRename().execute(ctx, name="S", new_name="T"),
        SectionSetMargins().execute(ctx, name="S", left_mm=5),
    ]
    for res in calls:
        assert res["status"] == "error"
        assert res["code"] == expected_code

    section.setPropertyValue.assert_not_called()
    section.setPropertyValues.assert_not_called()


class _ByteSequence:
    """Stand-in for pyuno.ByteSequence in pytest where `uno` is mocked as MagicMock."""

    def __init__(self, value: bytes) -> None:
        self._value = value

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, bytes) and self._value == other


def test_section_set_password_wrong_current_password_is_refused() -> None:
    section = MagicMock()
    right_key = _ByteSequence(hashlib.sha1("right".encode("utf-16-le"), usedforsecurity=False).digest())

    def _get_prop(prop_name: str) -> object:
        if prop_name == "DocumentIndex":
            return None
        if prop_name == "ProtectionKey":
            return right_key
        raise AssertionError(f"Unexpected property read: {prop_name}")

    section.getPropertyValue.side_effect = _get_prop
    doc = MagicMock()
    supplier = doc.getTextSections.return_value
    supplier.hasByName.return_value = True
    supplier.getByName.return_value = section

    res = SectionSetPassword().execute(_mock_ctx(doc=doc), name="S", password="new", current_password="wrong")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["details"]["parameter"] == "current_password"
    section.setPropertyValue.assert_not_called()
    section.setPropertyValues.assert_not_called()


@pytest.mark.parametrize(
    ("algo", "encoding"),
    [
        ("sha1", "utf-8"),
        ("sha1", "utf-16-le"),
        ("sha1", "utf-16-be"),
        ("sha256", "utf-8"),
    ],
)
def test_password_matches_libreoffice_hash_variants(algo: str, encoding: str) -> None:
    pw = "Gehe1m-ü"
    key = _ByteSequence(getattr(hashlib, algo)(pw.encode(encoding, "surrogatepass")).digest())
    assert _password_matches(key, pw) is True
    assert _password_matches(key, "wrong") is False
    assert _password_matches(_ByteSequence(b""), pw) is False
    assert _password_matches(_ByteSequence(b"\x01" * 16), pw) is False
    assert _hash_password("pw") == hashlib.sha1("pw".encode("utf-16-le"), usedforsecurity=False).digest()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"left_mm": 1000},
        {"right_mm": -1000},
        {"left_mm": float("inf")},
        {"right_mm": float("nan")},
    ],
)
def test_section_set_margins_rejects_bad_values_before_touching_doc(kwargs: dict[str, object]) -> None:
    doc = MagicMock()
    res = SectionSetMargins().execute(_mock_ctx(doc=doc), name="S", **kwargs)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    doc.getTextSections.assert_not_called()


def test_section_rename_rejects_empty_and_taken_names() -> None:
    doc = MagicMock()
    res_empty = SectionRename().execute(_mock_ctx(doc=doc), name="S", new_name="")
    assert res_empty["status"] == "error"
    assert res_empty["code"] == "INVALID_PARAM"
    assert res_empty["details"]["parameter"] == "new_name"
    doc.getTextSections.assert_not_called()

    section = MagicMock()
    section.getPropertyValue.side_effect = lambda p: None if p == "DocumentIndex" else b""
    supplier = doc.getTextSections.return_value
    supplier.hasByName.return_value = True
    supplier.getByName.return_value = section

    res_taken = SectionRename().execute(_mock_ctx(doc=doc), name="S", new_name="Other")
    assert res_taken["status"] == "error"
    assert res_taken["code"] == "INVALID_PARAM"
    assert res_taken["details"]["parameter"] == "new_name"
    section.setName.assert_not_called()


class _Enum:
    def __init__(self, items: list[object]) -> None:
        self.items = items
        self.i = 0

    def hasMoreElements(self) -> bool:
        return self.i < len(self.items)

    def nextElement(self) -> object:
        item = self.items[self.i]
        self.i += 1
        return item


class _Para:
    def __init__(self, text: str) -> None:
        self.text = text

    def supportsService(self, service: str) -> bool:
        return service == "com.sun.star.text.Paragraph"


class _Table:
    def __init__(self, cells: dict[str, _Cell]) -> None:
        self.cells = cells

    def supportsService(self, service: str) -> bool:
        return service == "com.sun.star.text.TextTable"

    def getCellNames(self) -> list[str]:
        return list(self.cells)

    def getCellByName(self, name: str) -> _Cell:
        return self.cells[name]


class _Cell:
    def __init__(self, text: str) -> None:
        self.text = text


class _Section:
    def __init__(self, enum_items: list[object]) -> None:
        self.enum_items = enum_items

    def getAnchor(self) -> MagicMock:
        anchor = MagicMock()
        anchor.createEnumeration.return_value = _Enum(self.enum_items)
        return anchor


class _Sections:
    def __init__(self, sections_dict: dict[str, _Section]) -> None:
        self.sections = sections_dict

    def hasByName(self, name: str) -> bool:
        return name in self.sections

    def getByName(self, name: str) -> _Section:
        return self.sections[name]

    def getElementNames(self) -> list[str]:
        return list(self.sections.keys())


def test_section_read_includes_table_content_and_accepts_name_or_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "plugin.writer.specialized.sections.get_string_without_tracked_deletions",
        lambda obj: obj.text,
    )
    items: list[object] = [_Para("Intro"), _Table({"A1": _Cell("A1 text"), "B1": _Cell("B1 text")})]
    sections_map = _Sections({"S": _Section(items)})
    doc = MagicMock()
    doc.getTextSections.return_value = sections_map
    ctx = _mock_ctx(doc=doc)

    res_by_name = SectionRead().execute(ctx, name="S")
    assert res_by_name["status"] == "ok"
    assert res_by_name["paragraphs"] == ["Intro", "A1 text\nB1 text"]
    assert res_by_name["content"] == "Intro\nA1 text\nB1 text"
    assert res_by_name["length"] == len(res_by_name["content"])

    res_by_alias = SectionRead().execute(ctx, section="S")
    assert res_by_alias == res_by_name


def test_section_read_missing_section() -> None:
    sections_map = _Sections({"Intro": _Section([])})
    doc = MagicMock()
    doc.getTextSections.return_value = sections_map
    ctx = _mock_ctx(doc=doc)

    res = SectionRead().execute(ctx, name="Intre")
    assert res["status"] == "error"
    assert "Section 'Intre' not found." in res["message"]
    assert res["details"]["available"] == ["Intro"]
    assert "section_list" in res["details"]["hint"]
    assert "Intro" in res["details"]["similar"]


def test_section_list_reports_index_password_and_columns() -> None:
    def _make_section(*, visible: bool, protected: bool, doc_index: object, prot_key: bytes, cols: int) -> MagicMock:
        sec = MagicMock()
        sec.IsVisible = visible
        sec.IsProtected = protected
        cols_obj = MagicMock()
        cols_obj.getColumnCount.return_value = cols

        def _get_prop(prop_name: str) -> object:
            if prop_name == "DocumentIndex":
                return doc_index
            if prop_name == "ProtectionKey":
                return prot_key
            if prop_name == "TextColumns":
                return cols_obj
            raise AssertionError(f"Unexpected property read: {prop_name}")

        sec.getPropertyValue.side_effect = _get_prop
        return sec

    by_name = {
        "Body": _make_section(visible=True, protected=False, doc_index=None, prot_key=b"", cols=0),
        "TOC1_Head": _make_section(visible=True, protected=True, doc_index=MagicMock(), prot_key=b"", cols=1),
        "Secret": _make_section(visible=False, protected=True, doc_index=None, prot_key=b"\x01" * 20, cols=3),
    }
    supplier = MagicMock()
    supplier.getElementNames.return_value = list(by_name.keys())
    supplier.getByName.side_effect = lambda n: by_name[n]

    doc = MagicMock()
    doc.getTextSections.return_value = supplier
    res = SectionList().execute(_mock_ctx(doc=doc))
    assert res["status"] == "ok"
    assert res["count"] == 3
    assert res["sections"] == [
        {
            "name": "Body",
            "is_visible": True,
            "is_protected": False,
            "is_index": False,
            "has_password": False,
            "column_count": 1,
        },
        {
            "name": "TOC1_Head",
            "is_visible": True,
            "is_protected": True,
            "is_index": True,
            "has_password": False,
            "column_count": 1,
        },
        {
            "name": "Secret",
            "is_visible": False,
            "is_protected": True,
            "is_index": False,
            "has_password": True,
            "column_count": 3,
        },
    ]


def test_registry_delegate_toolsets_for_sections_and_structural() -> None:
    registry = ToolRegistry(ServiceRegistry())
    for tool_cls in (
        SectionList,
        SectionGetInfo,
        SectionRead,
        SectionCreate,
        SectionDelete,
        SectionRename,
        SectionSetColumns,
        SectionSetMargins,
        SectionSetProtection,
        SectionSetPassword,
        SectionSetVisibility,
        NavGotoPage,
        SpecializedWorkflowFinished,
    ):
        registry.register(tool_cls())

    writer_services = ["com.sun.star.text.TextDocument"]
    sections_names = {
        t.name
        for t in registry.get_tools(
            doc_type="writer",
            uno_services_supported=writer_services,
            active_domain="sections",
        )
    }
    assert {
        "section_list",
        "section_get_info",
        "section_read",
        "section_create",
        "section_delete",
        "section_rename",
        "section_set_columns",
        "section_set_margins",
        "section_set_protection",
        "section_set_password",
        "section_set_visibility",
    } <= sections_names

    structural_names = {
        t.name
        for t in registry.get_tools(
            doc_type="writer",
            uno_services_supported=writer_services,
            active_domain="structural",
        )
    }
    assert {"section_list", "nav_goto_page"} <= structural_names
    assert "section_read" not in structural_names
    assert "section_create" not in structural_names
    assert "section_get_info" not in structural_names


def test_section_password_params_are_secret_keys() -> None:
    from plugin.framework.log_redaction import is_secret_key

    props = SectionSetPassword().parameters["properties"]
    pw_keys = [k for k in props if "password" in k]
    assert len(pw_keys) == 2
    assert all(is_secret_key(k) for k in pw_keys)
