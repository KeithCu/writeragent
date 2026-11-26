# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small file and git helpers shared by the tests of ``scripts/``."""

from __future__ import annotations

import subprocess
from pathlib import Path

from docx import Document
from odf.opendocument import OpenDocumentText
from odf.text import P


def write_docx(path: Path, text: str) -> Path:
    """Save *text* as a .docx with one plain paragraph per line."""
    doc = Document()
    for line in text.strip().splitlines():
        doc.add_paragraph(line)
    doc.save(str(path))
    return path


def write_odt(path: Path, text: str) -> Path:
    """Save *text* as an .odt with one plain paragraph per line, creating the parent folder."""
    doc = OpenDocumentText()
    for line in text.strip().splitlines():
        doc.text.addElement(P(text=line))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def git(cwd: Path, *args: str) -> None:
    """Run ``git *args`` in *cwd*, raising on a non-zero exit."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
