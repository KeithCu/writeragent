# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc/Excel formula tokenizer, parser, and AST nodes."""

from __future__ import annotations

import pytest

from plugin.contrib.calc_formula_parser import (
    FormulaParser,
    OperandNode,
    OperatorNode,
    RangeNode,
    parse_formula,
)
from plugin.contrib.calc_formula_parser.tokenizer import ExcelParser, f_token


def test_trailing_whitespace_no_crash():
    # Bug 1: trailing whitespace crashed with IndexError in currentChar() before EOF() check
    tokens = FormulaParser().tokenize("=A1 ")
    assert len(tokens) >= 1
    ast = parse_formula("=A1 \t\r\n")
    assert isinstance(ast, RangeNode)
    assert ast.tvalue == "A1"


def test_trailing_comma_no_crash():
    # Bug 2: trailing comma crashed with IndexError in currentChar()
    ast = parse_formula("=SUM(A1,)")
    assert ast.tvalue == "SUM"
    assert len(ast.args) == 2
    assert ast.args[0].tvalue == "A1"
    assert ast.args[1].tvalue == "None"


def test_empty_arguments_counted_correctly():
    # Bug 3: F(,1) and F(1,) must count 2 arguments and emit None operands
    ast1 = parse_formula("=IF(,1)")
    assert ast1.tvalue == "IF"
    assert len(ast1.args) == 2
    assert ast1.args[0].tvalue == "None"
    assert ast1.args[1].tvalue == "1"

    ast2 = parse_formula("=IF(1,)")
    assert ast2.tvalue == "IF"
    assert len(ast2.args) == 2
    assert ast2.args[0].tvalue == "1"
    assert ast2.args[1].tvalue == "None"

    ast3 = parse_formula("=IF(,,)")
    assert ast3.tvalue == "IF"
    assert len(ast3.args) == 3


def test_percent_postfix_operator():
    # Bug 4 & 6: % is a postfix operator that binds tighter than ^ and * /
    # A1% on cell reference
    ast = parse_formula("=A1%")
    assert isinstance(ast, OperatorNode)
    assert ast.ttype == "operator-postfix"
    assert ast.tvalue == "%"
    assert ast.left.tvalue == "A1"

    # (1+2)% on subexpression
    ast = parse_formula("=(1+2)%")
    assert isinstance(ast, OperatorNode)
    assert ast.ttype == "operator-postfix"
    assert ast.tvalue == "%"
    assert ast.left.tvalue == "+"

    # 2^50%: % (precedence 6) binds tighter than ^ (precedence 5)
    # The root should be ^, and its right child should be 50%
    ast = parse_formula("=2^50%")
    assert isinstance(ast, OperatorNode)
    assert ast.tvalue == "^"
    assert ast.left.tvalue == "2"
    assert isinstance(ast.right, OperatorNode)
    assert ast.right.ttype == "operator-postfix"
    assert ast.right.tvalue == "%"
    assert ast.right.left.tvalue == "50"

    # 2*50%: root should be *, right child 50%
    ast = parse_formula("=2*50%")
    assert isinstance(ast, OperatorNode)
    assert ast.tvalue == "*"
    assert ast.right.ttype == "operator-postfix"
    assert ast.right.tvalue == "%"

    # -50%: prefix minus binds tighter than %
    ast = parse_formula("=-50%")
    assert isinstance(ast, OperatorNode)
    assert ast.ttype == "operator-postfix"
    assert ast.tvalue == "%"
    assert ast.left.ttype == "operator-prefix"


def test_scientific_notation_signed_exponent():
    # Bug 5: scientific notation with signed exponents tokenizes as single number
    cases = ["=0.5E+3", "=10E+3", "=1.E+3", "=12e-3", "=1E+6", "=2.5e+2"]
    for f in cases:
        ast = parse_formula(f)
        assert isinstance(ast, OperandNode), f"Failed for {f}"
        assert ast.tsubtype == "number", f"Failed subtype for {f}"


def test_stray_closing_paren_raises_syntax_error():
    # Bug 6 in parser review: stray closing paren raises clear SyntaxError, not unhandled IndexError
    with pytest.raises(SyntaxError):
        parse_formula("=A1)")

    with pytest.raises(SyntaxError):
        parse_formula("=SUM(A1))")


def test_operand_classification():
    # Review item 8: nan, inf, and strings with underscore must not be classified as numbers
    for ident in ["nan", "inf", "-inf", "+inf", "col_a", "var_1"]:
        tokens = FormulaParser().tokenize(f"={ident}")
        op_tok = [t for t in tokens if t.ttype == ExcelParser.TOK_TYPE_OPERAND][0]
        assert op_tok.tsubtype != ExcelParser.TOK_SUBTYPE_NUMBER, f"{ident} misclassified as number"


def test_boolean_case_insensitivity():
    # Review item 9: true and false are case-insensitive
    for val in ["true", "false", "True", "False", "TRUE", "FALSE"]:
        ast = parse_formula(f"={val}")
        assert isinstance(ast, OperandNode)
        assert ast.tsubtype == "logical"
        assert str(ast) in ("TRUE", "FALSE")


def test_calc_sheet_syntax_and_quoted_names():
    # Review item 11 & Bug 7: Calc $'Sheet'.A1 syntax and quoted sheet names
    ast = parse_formula("=$'My Sheet'.A1")
    assert isinstance(ast, RangeNode)
    assert "$'My Sheet'.A1" in ast.tvalue

    ast2 = parse_formula("='My Sheet'!A1")
    assert isinstance(ast2, RangeNode)
    assert "'My Sheet'!A1" in ast2.tvalue


def test_unknown_token_raises_value_error():
    # Parser review item 1: unknown tokens must raise ValueError instead of vanishing silently
    parser = FormulaParser()
    toks = parser.tokenize("=A1")
    toks.append(f_token("???", "unknown", ""))
    with pytest.raises(ValueError, match="Unknown or invalid token"):
        parser.shunting_yard(toks, {})


def test_prefix_operator_in_union():
    # Parser review item 2: u- does not pop union comma in (A1,-B1)
    ast = parse_formula("=(A1,-B1)")
    assert isinstance(ast, OperatorNode)
    assert ast.tvalue == ","
    assert ast.left.tvalue == "A1"
    assert ast.right.ttype == "operator-prefix"
    assert ast.right.tvalue == "-"
    assert ast.right.right.tvalue == "B1"


def test_offset_index_range_extensions():
    # Parser review item 4: A1:OFFSET(...), A1:INDEX(...), A1:INDIRECT(...)
    ast1 = parse_formula("=A1:OFFSET(B1,1,1)")
    assert isinstance(ast1, RangeNode)
    assert ast1.tvalue == "A1:OFFSET(B1,1,1)"

    ast2 = parse_formula("=A1:INDEX(B1:C5,2,2)")
    assert isinstance(ast2, RangeNode)
    assert ast2.tvalue == "A1:INDEX(B1:C5,2,2)"

    ast3 = parse_formula('=A1:INDIRECT("B5")')
    assert isinstance(ast3, RangeNode)
    assert 'A1:INDIRECT("B5")' in ast3.tvalue

    ast4 = parse_formula("=A1:CHOOSE(1,B1,C1)")
    assert isinstance(ast4, RangeNode)
    assert ast4.tvalue == "A1:CHOOSE(1,B1,C1)"


def test_ast_node_str_escaping():
    # Review ast_nodes item: OperandNode string representation escapes quotes as ""
    tok = f_token('"hello" world', "operand", "text")
    text_node = OperandNode(tok)
    assert str(text_node) == '"""hello"" world"'

    # Postfix operator str formatting
    op_tok = f_token("%", "operator-postfix", "")
    op_node = OperatorNode(op_tok)
    op_node.left = OperandNode(f_token(50, "operand", "number"))
    assert str(op_node) == "(50)%"
