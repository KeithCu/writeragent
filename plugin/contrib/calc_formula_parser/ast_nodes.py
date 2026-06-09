# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Portions Copyright (c) Bradley van Ree — xlcalculator (MIT); see README.md
"""Codegen-only formula AST nodes (no evaluation)."""

from __future__ import annotations


class ASTNode:
    """A generic node in the formula AST."""

    def __init__(self, token) -> None:
        self.token = token

    @property
    def tvalue(self):
        return self.token.tvalue

    @property
    def ttype(self):
        return self.token.ttype

    @property
    def tsubtype(self):
        return self.token.tsubtype

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"tvalue: {self.tvalue!r}, "
            f"ttype: {self.ttype}, "
            f"tsubtype: {self.tsubtype}>"
        )

    def __str__(self) -> str:
        return str(self.tvalue)

    def __iter__(self):
        yield self


class OperandNode(ASTNode):
    def __str__(self) -> str:
        if self.tsubtype == "logical":
            return self.tvalue.title()
        if self.tsubtype == "text":
            return '"' + str(self.tvalue).replace('"', '\\"') + '"'
        return str(self.tvalue)


class RangeNode(OperandNode):
    """Spreadsheet cell or range reference."""

    @property
    def address(self) -> str:
        return str(self.tvalue)


class OperatorNode(ASTNode):
    def __init__(self, token) -> None:
        super().__init__(token)
        self.left = None
        self.right = None

    def __str__(self) -> str:
        left = f"({self.left}) " if self.left is not None else ""
        right = f" ({self.right})" if self.right is not None else ""
        return f"{left}{self.tvalue}{right}"

    def __iter__(self):
        if self.left is not None:
            yield from self.left
        if self.right is not None:
            yield from self.right
        yield self


class FunctionNode(ASTNode):
    def __init__(self, token) -> None:
        super().__init__(token)
        self.args: list[ASTNode] | None = None
        self.num_args: int = 0

    def __str__(self) -> str:
        args = ", ".join(str(arg) for arg in (self.args or []))
        return f"{self.tvalue}({args})"

    def __iter__(self):
        for arg in self.args or []:
            yield from arg
        yield self
