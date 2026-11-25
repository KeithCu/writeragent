# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the synthetic redlined contract flat ODF generator."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from tests.writer.redlined_contract_fixture import build_redlined_contract

_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
_CHANGE_ID = f"{{{_TEXT_NS}}}change-id"
_TRACKED_TAG = f"{{{_TEXT_NS}}}tracked-changes"
_REGION_TAG = f"{{{_TEXT_NS}}}changed-region"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"articles": 4, "sections": 2, "clauses": 8, "tables": 2, "multi_every": 40},
    ],
)
def test_redlined_contract_xml_and_change_ids(kwargs):
    contract = build_redlined_contract(**kwargs)
    root = ET.fromstring(contract.xml)

    region_ids = [el.attrib[_XML_ID] for el in root.iter(_REGION_TAG)]
    assert len(region_ids) == contract.deletions + contract.insertions
    assert len(set(region_ids)) == len(region_ids)

    body_ids = {el.attrib[_CHANGE_ID] for el in root.iter() if _CHANGE_ID in el.attrib}
    assert body_ids == set(region_ids)


def test_redlined_contract_no_changes():
    contract = build_redlined_contract(redline_every=0)
    root = ET.fromstring(contract.xml)

    assert contract.deletions == 0
    assert contract.insertions == 0
    assert contract.multi_paragraph_deletions == 0
    assert list(root.iter(_TRACKED_TAG)) == []
    assert [el for el in root.iter() if _CHANGE_ID in el.attrib] == []
