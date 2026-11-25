# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic redlined contract as flat ODF, for UNO tests and the read benchmark.

Plain text generation, no UNO: the file carries another author's tracked changes
directly, so no profile name or RecordChanges state has to be switched to build it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AUTHOR = "Opposing Counsel"
DELETED = "obsolete wording "
INSERTED = "additional wording "
TABLE_ROWS, TABLE_COLS = 20, 4

_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" xmlns:dc="http://purl.org/dc/elements/1.1/" office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">
<office:styles>
<style:default-style style:family="paragraph"><style:text-properties fo:font-size="12pt" fo:language="en" fo:country="US"/></style:default-style>
<style:style style:name="Standard" style:family="paragraph" style:class="text"/>
<style:style style:name="Clause" style:family="paragraph" style:parent-style-name="Standard"><style:paragraph-properties fo:text-align="justify" fo:margin-bottom="0.25cm" fo:line-height="130%"/></style:style>
<style:style style:name="Heading_20_1" style:display-name="Heading 1" style:family="paragraph" style:parent-style-name="Standard" style:next-style-name="Clause" style:default-outline-level="1"><style:text-properties fo:font-size="16pt" fo:font-weight="bold"/></style:style>
<style:style style:name="Heading_20_2" style:display-name="Heading 2" style:family="paragraph" style:parent-style-name="Standard" style:next-style-name="Clause" style:default-outline-level="2"><style:text-properties fo:font-size="13pt" fo:font-weight="bold"/></style:style>
<text:outline-style style:name="Outline"><text:outline-level-style text:level="1" style:num-suffix="." style:num-format="1"/><text:outline-level-style text:level="2" text:display-levels="2" style:num-format="1"/></text:outline-style>
"""
_LIST_STYLE = '<text:list-style style:name="%s"><text:list-level-style-number text:level="1" style:num-prefix="(" style:num-suffix=")" style:num-format="a"><style:list-level-properties text:list-level-position-and-space-mode="label-alignment"><style:list-level-label-alignment text:label-followed-by="listtab" fo:text-indent="-0.6cm" fo:margin-left="1.2cm"/></style:list-level-properties></text:list-level-style-number></text:list-style>\n'
_PAGE = """</office:styles>
<office:automatic-styles>
<style:style style:name="Bold" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>
<style:page-layout style:name="pm1"><style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" fo:margin-top="2.5cm" fo:margin-bottom="2.5cm" fo:margin-left="2.5cm" fo:margin-right="2.5cm"/></style:page-layout>
</office:automatic-styles>
<office:master-styles><style:master-page style:name="Standard" style:page-layout-name="pm1"/></office:master-styles>
<office:body><office:text>
"""
_TOC = (
    '<text:table-of-content text:name="Contents" text:protected="true"><text:table-of-content-source text:outline-level="2">'
    "<text:index-title-template>Contents</text:index-title-template>"
    + "".join(
        '<text:table-of-content-entry-template text:outline-level="%d" text:style-name="Standard"><text:index-entry-chapter/><text:index-entry-text/>'
        '<text:index-entry-tab-stop style:type="right" style:leader-char="."/><text:index-entry-page-number/></text:table-of-content-entry-template>' % level
        for level in (1, 2)
    )
    + '</text:table-of-content-source><text:index-body><text:p text:style-name="Standard">Contents</text:p></text:index-body></text:table-of-content>\n'
)
_WORDS = (
    "the Supplier shall deliver the Services to the Customer in accordance with this Agreement and any applicable Schedule "
    "notwithstanding prior written notice of termination by either party under the governing law"
).split()


@dataclass(frozen=True)
class RedlinedContract:
    """Generated flat ODF contract and its expected change/table counts."""

    xml: str
    deletions: int
    insertions: int
    multi_paragraph_deletions: int
    table_names: tuple[str, ...]


@dataclass
class _Changes:
    regions: list[str] = field(default_factory=list)
    deletions: int = 0
    insertions: int = 0

    def _region(self, kind: str, content: str) -> str:
        rid = "ct%d" % (len(self.regions) + 1)
        self.regions.append(
            '<text:changed-region xml:id="%s" text:id="%s"><text:%s><office:change-info><dc:creator>%s</dc:creator>'
            "<dc:date>2026-01-02T03:04:05</dc:date></office:change-info>%s</text:%s></text:changed-region>\n" % (rid, rid, kind, AUTHOR, content, kind)
        )
        return rid

    def insert(self, text: str) -> str:
        self.insertions += 1
        rid = self._region("insertion", "")
        return '<text:change-start text:change-id="%s"/>%s<text:change-end text:change-id="%s"/>' % (rid, text, rid)

    def delete(self, *paragraphs: str, style: str = "Clause") -> str:
        self.deletions += 1
        content = "".join('<text:p text:style-name="%s">%s</text:p>' % (style, p) for p in paragraphs)
        return '<text:change text:change-id="%s"/>' % self._region("deletion", content)


def _sentence(number: int) -> str:
    return "Clause %d: %s." % (number, " ".join(_WORDS[(number * 7 + i) % len(_WORDS)] for i in range(70)))


def _cell(content: str, spans: str = "") -> str:
    return '<table:table-cell office:value-type="string"%s><text:p text:style-name="Standard">%s</text:p></table:table-cell>' % (spans, content)


def _table(name: str, changes: _Changes, redlined: bool) -> str:
    """One fee schedule: a title row merged across, one vertical merge, and redlined cells in column B."""
    out = ['<table:table table:name="%s"><table:table-column table:number-columns-repeated="%d"/>' % (name, TABLE_COLS)]
    for row in range(TABLE_ROWS):
        cells = ["cell %d %d " % (row, col) for col in range(TABLE_COLS)]
        if redlined and row % 4 == 2:
            cells[1] += changes.delete("old fee", style="Standard") + changes.insert("new fee")
        if row == 0:
            xml = _cell(name + " fees", ' table:number-columns-spanned="%d"' % TABLE_COLS) + "<table:covered-table-cell/>" * (TABLE_COLS - 1)
        elif row == 5:
            xml = _cell(cells[0], ' table:number-rows-spanned="2"') + "".join(_cell(c) for c in cells[1:])
        elif row == 6:
            xml = "<table:covered-table-cell/>" + "".join(_cell(c) for c in cells[1:])
        else:
            xml = "".join(_cell(c) for c in cells)
        out.append("<table:table-row>%s</table:table-row>" % xml)
    return "".join(out) + "</table:table>\n"


def build_redlined_contract(
    articles: int = 60,
    sections: int = 4,
    clauses: int = 7,
    tables: int = 6,
    redline_every: int = 8,
    multi_every: int = 400,
    shared_list: bool = False,
) -> RedlinedContract:
    """Build the contract. The defaults lay out to about 300 pages with about 480 changes.

    ``redline_every=0`` gives the same base text with no tracked changes. One clause in
    *redline_every* has a deletion and one an insertion; one in *multi_every* carries a
    deletion over three paragraphs. ``shared_list=True`` numbers every clause with one list
    style instead of one per article; the default keeps lists per-article because a long
    single list causes LibreOffice's ``odf2xhtml`` filter to abort at 3000 template
    recursions, making ``get_content`` fall back to StarWriter HTML instead of timing
    the XHTML route.
    """
    changes = _Changes()
    body: list[str] = []
    table_names: list[str] = []
    number = multi = 0
    for article in range(1, articles + 1):
        body.append('<text:h text:style-name="Heading_20_1" text:outline-level="1">Article %d Obligations</text:h>\n' % article)
        list_style = "Clauses" if shared_list else "Clauses%d" % article
        for section in range(1, sections + 1):
            body.append('<text:h text:style-name="Heading_20_2" text:outline-level="2">Section %d Terms</text:h>\n' % section)
            body.append('<text:list text:style-name="%s">' % list_style)
            for _clause in range(clauses):
                number += 1
                text = _sentence(number)
                head, tail = text[:40], text[40:]
                if redline_every and number % multi_every == multi_every // 2 + 1:
                    multi += 1
                    text = head + changes.delete("tail of one", "whole struck clause", "head of next ") + tail
                elif redline_every and number % redline_every == 0:
                    text = head + changes.delete(DELETED) + tail
                elif redline_every and number % redline_every == redline_every // 2:
                    text = head + changes.insert(INSERTED) + tail
                elif number % 8 == 1:
                    text = '<text:span text:style-name="Bold">%s</text:span>%s' % (head, tail)
                body.append('<text:list-item><text:p text:style-name="Clause">%s</text:p></text:list-item>' % text)
            body.append("</text:list>\n")
        if tables and article % (articles // tables) == 0:
            table_names.append("Schedule%d" % article)
            body.append(_table(table_names[-1], changes, bool(redline_every)))
    body.append('<text:p text:style-name="Clause">End of Agreement.</text:p></office:text></office:body></office:document>\n')
    list_names = ["Clauses"] if shared_list else ["Clauses%d" % a for a in range(1, articles + 1)]
    tracked = '<text:tracked-changes text:track-changes="false">%s</text:tracked-changes>\n' % "".join(changes.regions) if changes.regions else ""
    xml = _HEAD + "".join(_LIST_STYLE % name for name in list_names) + _PAGE + tracked + _TOC + "".join(body)
    return RedlinedContract(xml, changes.deletions, changes.insertions, multi, tuple(table_names))
