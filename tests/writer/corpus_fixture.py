# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Word-style .docx and flat ODF documents for the Writer corpus UNO suite.

Plain text generation, no UNO and no python-docx (LibreOffice's bundled Python does not have
it): each builder writes WordprocessingML modelled on what Word writes for that feature (not
saved by Word itself), so the documents go through LibreOffice's Word import filter.
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tests.testing_utils import logo_32_path
from tests.writer.redlined_contract_fixture import build_redlined_contract

OPPOSING = "Opposing Counsel"
ASSOCIATE = "Our Associate"
_OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006"
_PACKAGE = "http://schemas.openxmlformats.org/package/2006"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006"
_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    f'xmlns:r="{_OFFICE}/relationships" xmlns:wp="{_DRAWING}/wordprocessingDrawing" '
    f'xmlns:a="{_DRAWING}/main" xmlns:pic="{_DRAWING}/picture"'
)
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


@dataclass(frozen=True)
class CorpusDoc:
    """One corpus document: its builder, the sentence the edit test replaces, and what import must yield."""

    name: str
    suffix: str
    write: Callable[[Path], None]
    old: str
    new: str
    redline_authors: tuple[str, ...] = ()
    redlines: tuple[tuple[str, str], ...] = ()
    tables: int = 0
    indexes: int = 0
    header_images: tuple[str, ...] = ()
    footnotes: int = 0
    endnotes: int = 0
    comments: int = 0
    bookmarks: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()


class _Word:
    """Run, paragraph and table markup; numbers the tracked changes of one document."""

    def __init__(self) -> None:
        self._change = 0

    def change(self, author: str) -> str:
        self._change += 1
        return f'w:id="{self._change}" w:author="{author}" w:date="2026-01-02T03:04:05Z"'

    def ins(self, author: str, text: str) -> str:
        return f"<w:ins {self.change(author)}>{run(text)}</w:ins>"

    def delete(self, author: str, text: str) -> str:
        return f'<w:del {self.change(author)}><w:r><w:delText xml:space="preserve">{text}</w:delText></w:r></w:del>'

    def deleted_mark(self, author: str) -> str:
        """Paragraph properties for a tracked deletion of the paragraph mark."""
        return f"<w:rPr><w:del {self.change(author)}/></w:rPr>"


def run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def para(content: str, style: str = "Normal", props: str = "") -> str:
    number = '<w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr>' if style == "Clause" else ""
    return (
        f'<w:p w:rsidR="00A1B2C3"><w:pPr><w:pStyle w:val="{style}"/>{number}{props}</w:pPr>'
        f'<w:proofErr w:type="spellStart"/>{content}<w:proofErr w:type="spellEnd"/></w:p>'
    )


def field(instruction: str, shown: str) -> str:
    return (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> {instruction} </w:instrText></w:r>'
        f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{shown}<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )


def cell(content: str, props: str = "") -> str:
    return f'<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/>{props}</w:tcPr>{para(content)}</w:tc>'


def row(*cells: str, props: str = "") -> str:
    return f"<w:tr><w:trPr>{props}</w:trPr>{''.join(cells)}</w:tr>"


def table(*rows: str) -> str:
    grid = '<w:gridCol w:w="2400"/>' * 3
    return f'<w:tbl><w:tblPr><w:tblW w:w="7200" w:type="dxa"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(rows)}</w:tbl>'


def logo() -> str:
    return (
        '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="720000" cy="360000"/>'
        f'<wp:docPr id="1" name="Logo" descr="Firm logo"/><a:graphic><a:graphicData uri="{_DRAWING}/picture"><pic:pic>'
        '<pic:nvPicPr><pic:cNvPr id="1" name="Logo"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="rId1"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="720000" cy="360000"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )


def table_of_contents(*headings: str) -> str:
    """The content-control wrapped TOC field Word inserts, with its cached entries."""
    entries = [run(f"{n}. {text}") + "<w:r><w:tab/></w:r>" + run("1") for n, text in enumerate(headings, 1)]
    start = (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-1" \\h \\z \\u </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    )
    body = para(start + entries[0], "TOC1") + "".join(para(entry, "TOC1") for entry in entries[1:])
    return (
        '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/><w:docPartUnique/></w:docPartObj></w:sdtPr>'
        f'<w:sdtContent>{body}<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:sdtContent></w:sdt>'
    )


_STYLES = (
    '<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="22"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Clause"/>'
    '<w:pPr><w:keepNext/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="TOC1"><w:name w:val="toc 1"/><w:basedOn w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Header"><w:name w:val="header"/><w:basedOn w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:customStyle="1" w:styleId="Clause"><w:name w:val="Clause"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:spacing w:after="120"/><w:jc w:val="both"/></w:pPr></w:style>'
)
_NUMBERING = (
    '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:pStyle w:val="Heading1"/><w:lvlText w:val="%1."/>'
    '<w:pPr><w:ind w:left="432" w:hanging="432"/></w:pPr></w:lvl>'
    '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/>'
    '<w:pPr><w:ind w:left="720" w:hanging="720"/></w:pPr></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
)


def write_docx(path: Path, body: str, *, header: str = "", footer: str = "", notes: dict[str, str] | None = None) -> None:
    """Write a .docx. *notes* maps ``footnotes`` / ``endnotes`` / ``comments`` to that part's content."""
    # part name -> (root element, content)
    xml_parts = {"styles": ("styles", _STYLES), "numbering": ("numbering", _NUMBERING), "settings": ("settings", "")}
    xml_parts.update({name: (name, content) for name, content in (notes or {}).items()})
    references = ""
    for name, root, content in (("header", "hdr", header), ("footer", "ftr", footer)):
        if content:
            xml_parts[name] = (root, content)
            references += f'<w:{name}Reference w:type="default" r:id="rId_{name}"/>'
    relation = f'<Relationship Id="%s" Type="{_OFFICE}/relationships/%s" Target="%s"/>'
    relations = f'<Relationships xmlns="{_PACKAGE}/relationships">%s</Relationships>'
    override = '<Override PartName="/word/%s.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.%s+xml"/>'
    page = '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708"/>'
    parts = {
        "[Content_Types].xml": f'<Types xmlns="{_PACKAGE}/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        + override % ("document", "document.main") + "".join(override % (name, name) for name in xml_parts) + "</Types>",
        "_rels/.rels": relations % (relation % ("rId1", "officeDocument", "word/document.xml")),
        "word/_rels/document.xml.rels": relations % "".join(relation % ("rId_" + name, name, name + ".xml") for name in xml_parts),
        "word/document.xml": f"<w:document {_NS}><w:body>{body}<w:sectPr>{references}{page}</w:sectPr></w:body></w:document>",
    }
    if header:
        parts["word/_rels/header.xml.rels"] = relations % (relation % ("rId1", "image", "media/logo.png"))
    parts.update({f"word/{name}.xml": f"<w:{root} {_NS}>{content}</w:{root}>" for name, (root, content) in xml_parts.items()})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            archive.writestr(name, _XML + xml)
        if header:
            archive.write(logo_32_path(), "word/media/logo.png")


_PAGE_FOOTER = para(run("Page ") + field("PAGE", run("1")), "Header")


def write_contract(path: Path, *, struck_before_heading: bool = False) -> None:
    """Numbered justified clauses, two authors' changes, a merged redlined table, a TOC and a logo header."""
    w = _Word()
    struck = para(w.delete(ASSOCIATE, "This whole clause is struck."), "Clause", w.deleted_mark(ASSOCIATE))
    kept = para(run("Delivery occurs at the ") + w.delete(ASSOCIATE, "old ") + run("warehouse."), "Clause")
    body = (
        para(run("MASTER SERVICES AGREEMENT"))
        + table_of_contents("Definitions", "Services", "Fees")
        + para(run("Definitions"), "Heading1")
        + para(run("The term ") + w.delete(OPPOSING, "obsolete ") + w.ins(OPPOSING, "updated ") + run("Agreement means this document."), "Clause")
        + para(run("The Customer may ") + w.ins(ASSOCIATE, "not ") + run("assign this Agreement without consent."), "Clause")
        + para(run("Services"), "Heading1")
        + para(run("The Supplier shall deliver the Services promptly."), "Clause")
        # Reverse order triggers F1 (struck clause before Heading1).
        + (kept + struck if struck_before_heading else struck + kept)
        + para(run("Fees"), "Heading1")
        + para(run("Fees are payable within thirty days."), "Clause")
        + table(
            row(cell(run("Fee schedule"), '<w:gridSpan w:val="3"/>')),
            row(cell(run("Phase"), '<w:vMerge w:val="restart"/>'), cell(run("Design")), cell(w.delete(OPPOSING, "100") + w.ins(OPPOSING, "200"))),
            row(cell("", "<w:vMerge/>"), cell(run("Build")), cell(run("300"))),
        )
        + para(run("End of Agreement."))
    )
    # No space between logo and text: a space comes back as a no-break space (F7).
    write_docx(path, body, header=para(logo() + run("Acme LLP"), "Header"), footer=_PAGE_FOOTER)


def _write_row_changes(path: Path) -> None:
    """Inserted and deleted table rows, a format change, and a deleted paragraph break."""
    w = _Word()
    bold = f"<w:r><w:rPr><w:b/><w:rPrChange {w.change(OPPOSING)}><w:rPr/></w:rPrChange></w:rPr><w:t>Bolded by counsel.</w:t></w:r>"
    body = (
        table(
            row(cell(run("Item")), cell(run("Qty")), cell(run("Price"))),
            row(*(cell(w.ins(OPPOSING, text)) for text in ("New", "1", "10")), props=f"<w:ins {w.change(OPPOSING)}/>"),
            row(*(cell(w.delete(ASSOCIATE, text)) for text in ("Old", "2", "20")), props=f"<w:del {w.change(ASSOCIATE)}/>"),
            row(cell(run("Kept")), cell(run("3")), cell(run("30"))),
        )
        + para(bold)
        + para(run("First part ") + w.delete(ASSOCIATE, "tail"), props=w.deleted_mark(ASSOCIATE))
        + para(w.delete(ASSOCIATE, "head ") + run("second part."))
        + para(run("Plain closing text."))
    )
    write_docx(path, body)


def _write_notes(path: Path) -> None:
    """A footnote, endnote, comment, bookmark cross-reference, content control and one insertion."""
    w = _Word()
    body = (
        para('<w:bookmarkStart w:id="50" w:name="_Ref1"/>' + run("Definitions") + '<w:bookmarkEnd w:id="50"/>', "Heading1")
        + para(run("See note") + '<w:r><w:footnoteReference w:id="2"/></w:r>' + run(" and end") + '<w:r><w:endnoteReference w:id="2"/></w:r>' + run("."), "Clause")
        + para(
            '<w:commentRangeStart w:id="0"/>' + run("Commented words") + '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r>'
            + w.ins(OPPOSING, " inserted") + run(" here."), "Clause")
        + para(
            run("Clause ") + field("REF _Ref1 \\h", run("Definitions")) + run(" applies from ")
            + '<w:sdt><w:sdtPr><w:alias w:val="Date"/><w:text/></w:sdtPr><w:sdtContent>' + run("1 May 2026") + "</w:sdtContent></w:sdt>" + run("."), "Clause")
        + para(run("Plain closing clause."), "Clause")
    )
    notes = {}
    for kind in ("footnote", "endnote"):
        separators = (
            f'<w:{kind} w:type="separator" w:id="0"><w:p><w:r><w:separator/></w:r></w:p></w:{kind}>'
            f'<w:{kind} w:type="continuationSeparator" w:id="1"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:{kind}>'
        )
        notes[kind + "s"] = separators + f'<w:{kind} w:id="2"><w:p><w:r><w:{kind}Ref/></w:r>{run(kind + " text.")}</w:p></w:{kind}>'
    notes["comments"] = f'<w:comment w:id="0" w:author="{OPPOSING}" w:date="2026-01-02T03:04:05Z" w:initials="OC"><w:p>{run("Please confirm.")}</w:p></w:comment>'
    write_docx(path, body, notes=notes)


def _write_letter(path: Path) -> None:
    """No tracked changes; the letterhead is a header table with the logo."""
    header = table(row(cell(logo()), cell(run("Acme LLP")), cell(run("1 High St")))) + para("", "Header")
    body = para(run("Dear Counsel,")) + para(run("We write about the agreement.")) + para(run("Yours faithfully"))
    write_docx(path, body, header=header, footer=_PAGE_FOOTER)


def _write_redlined_odf(path: Path) -> None:
    """The benchmark contract, small: redlines in a merged table and a three-paragraph deletion."""
    path.write_text(build_redlined_contract(articles=1, sections=2, clauses=8, tables=1, multi_every=12).xml, encoding="utf-8")


_SECTIONS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    ' xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">'
    '<office:automatic-styles><style:style style:name="S1" style:family="section"/></office:automatic-styles><office:body><office:text>'
    "<text:p>Intro paragraph.</text:p>"
    '<text:section text:style-name="S1" text:name="Locked" text:protected="true"><text:p>Locked clause text.</text:p></text:section>'
    "<text:p>Between text.</text:p>"
    '<text:section text:style-name="S1" text:name="Secret" text:display="none"><text:p>Hidden clause text.</text:p></text:section>'
    "<text:p>Closing paragraph.</text:p></office:text></office:body></office:document>"
)


def _write_sections(path: Path) -> None:
    """One protected and one hidden section."""
    path.write_text(_SECTIONS, encoding="utf-8")


CONTRACT = CorpusDoc(
    "contract", ".docx", write_contract, "Fees are payable within thirty days.", "Fees are payable within sixty days.",
    redline_authors=(OPPOSING, ASSOCIATE),
    redlines=(
        ("Delete", OPPOSING), ("Delete", ASSOCIATE), ("Delete", ASSOCIATE), ("Delete", OPPOSING),
        ("Insert", OPPOSING), ("Insert", ASSOCIATE), ("Insert", OPPOSING),
    ),
    tables=1, indexes=1, header_images=("Logo",), sections=("Table of Contents1",),
)
ROW_CHANGES = CorpusDoc(
    "row_changes", ".docx", _write_row_changes, "Plain closing text.", "Changed closing text.",
    redline_authors=(OPPOSING, ASSOCIATE),
    redlines=(
        ("Insert", OPPOSING), ("Insert", OPPOSING), ("Insert", OPPOSING),
        ("Delete", ASSOCIATE), ("Delete", ASSOCIATE), ("Delete", ASSOCIATE), ("Delete", ASSOCIATE),
        ("Format", OPPOSING),
    ),
    tables=1,
)
NOTES = CorpusDoc(
    "notes", ".docx", _write_notes, "Plain closing clause.", "Changed closing clause.",
    redline_authors=(OPPOSING,), redlines=(("Insert", OPPOSING),),
    footnotes=1, endnotes=1, comments=1, bookmarks=("_Ref1",),
)
LETTER = CorpusDoc("letter", ".docx", _write_letter, "We write about the agreement.", "We write about the contract.", header_images=("Logo",))
SECTIONS = CorpusDoc(
    "sections", ".fodt", _write_sections, "Closing paragraph.", "Changed paragraph.",
    sections=("Locked", "Secret"),
)
REDLINED_ODF = CorpusDoc(
    "redlined_odf", ".fodt", _write_redlined_odf, "End of Agreement.", "End of this Agreement.",
    redline_authors=(OPPOSING,),
    redlines=(("Delete", OPPOSING),) * 8 + (("Insert", OPPOSING),) * 7,
    tables=1, indexes=1, sections=("Table of Contents1",),
)
