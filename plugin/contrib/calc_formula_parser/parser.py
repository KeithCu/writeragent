# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Portions Copyright (c) Bradley van Ree — xlcalculator (MIT); see README.md
"""Shunting-yard formula parser → AST."""

from __future__ import annotations

from typing import NamedTuple

from plugin.contrib.calc_formula_parser import ast_nodes, tokenizer


class Operator(NamedTuple):
    value: str
    precedence: int
    associativity: str


# Excel operator precedence (Microsoft docs).
OPERATORS = {
    ":": Operator(":", 8, "left"),
    "": Operator(" ", 8, "left"),
    "intersect": Operator(" ", 8, "left"),
    ",": Operator(",", 8, "left"),
    "u-": Operator("u-", 7, "right"),
    "%": Operator("%", 6, "left"),
    "^": Operator("^", 5, "left"),
    "*": Operator("*", 4, "left"),
    "/": Operator("/", 4, "left"),
    "+": Operator("+", 3, "left"),
    "-": Operator("-", 3, "left"),
    "&": Operator("&", 2, "left"),
    "=": Operator("=", 1, "left"),
    "<": Operator("<", 1, "left"),
    ">": Operator(">", 1, "left"),
    "<=": Operator("<=", 1, "left"),
    ">=": Operator(">=", 1, "left"),
    "<>": Operator("<>", 1, "left"),
}


class FormulaParser:
    """Parse a worksheet formula string into an AST."""

    def parse(self, formula: str, named_ranges: dict[str, str] | None = None, *, tokenize_range: bool = False) -> ast_nodes.ASTNode:
        named_ranges = named_ranges or {}
        tokens = self.tokenize(formula, tokenize_range=tokenize_range)
        nodes = self.shunting_yard(tokens, named_ranges, tokenize_range=tokenize_range)
        return self.build_ast(nodes)

    def tokenize(self, formula: str, *, tokenize_range: bool = False) -> list:
        if formula.startswith("="):
            formula = formula[1:]
        excel_parser = tokenizer.ExcelParser(tokenize_range=tokenize_range)
        return excel_parser.parse(formula).items

    def shunting_yard(self, raw_tokens, named_ranges: dict[str, str], *, tokenize_range: bool = False) -> list:
        tokens: list = []
        for token in raw_tokens:
            if token.ttype == "function" and token.tsubtype == "start":
                token.tsubtype = ""
                tokens.append(token)
                tokens.append(tokenizer.f_token("(", "arglist", "start"))
            elif token.ttype == "function" and token.tsubtype == "stop":
                tokens.append(tokenizer.f_token(")", "arglist", "stop"))
            elif token.ttype == "subexpression" and token.tsubtype == "start":
                token.tvalue = "("
                tokens.append(token)
            elif token.ttype == "subexpression" and token.tsubtype == "stop":
                token.tvalue = ")"
                tokens.append(token)
            elif token.ttype == "operand" and token.tsubtype == "range" and token.tvalue in named_ranges:
                token.tvalue = named_ranges[token.tvalue]
                tokens.append(token)
            else:
                tokens.append(token)

        output: list = []
        stack: list = []
        were_values: list[bool] = []
        arg_count: list[int] = []
        new_tokens: list = []

        if not tokenize_range:
            index = 0
            while index < len(tokens):
                token = tokens[index]
                if not isinstance(token.tvalue, str):
                    new_tokens.append(token)
                    index += 1
                    continue
                if token.tvalue.startswith(":"):
                    depth = 0
                    expr = ""
                    while new_tokens:
                        t = new_tokens.pop()
                        if t.tsubtype == "stop":
                            depth += 1
                        elif depth > 0 and t.tsubtype == "start":
                            depth -= 1
                        val = f'"{t.tvalue}"' if t.tsubtype == "text" else str(t.tvalue)
                        expr = val + expr
                        if depth == 0:
                            break
                    expr += token.tvalue
                    depth = 0
                    index += 1
                    fn_name = token.tvalue[1:].upper()
                    if fn_name in ("OFFSET", "INDEX", "INDIRECT", "CHOOSE"):
                        while index < len(tokens):
                            t = tokens[index]
                            index += 1
                            if t.tsubtype == "start":
                                depth += 1
                            elif depth > 0 and t.tsubtype == "stop":
                                depth -= 1
                            val = f'"{t.tvalue}"' if t.tsubtype == "text" else str(t.tvalue)
                            expr += val
                            if depth == 0:
                                break
                    new_tokens.append(tokenizer.f_token(expr, "operand", "pointer"))
                elif any(f":{fn}" in token.tvalue.upper() for fn in ("OFFSET", "INDEX", "INDIRECT", "CHOOSE")):
                    depth = 0
                    expr = token.tvalue
                    index += 1
                    while index < len(tokens):
                        t = tokens[index]
                        index += 1
                        if t.tsubtype == "start":
                            depth += 1
                        elif t.tsubtype == "stop":
                            depth -= 1
                        val = f'"{t.tvalue}"' if t.tsubtype == "text" else str(t.tvalue)
                        expr += val
                        if depth == 0:
                            break
                    new_tokens.append(tokenizer.f_token(expr, "operand", "pointer"))
                else:
                    new_tokens.append(token)
                    index += 1

        tokens = new_tokens if new_tokens else tokens

        for token in tokens:
            if token.ttype == "unknown":
                # Bugfix: unknown tokens previously vanished silently in shunting_yard with no error.
                raise ValueError(f"Unknown or invalid token in formula: {token.tvalue!r}")
            elif token.ttype == "operand":
                output.append(self.create_node(token))
                if were_values:
                    were_values.pop()
                    were_values.append(True)
            elif token.ttype == "function":
                stack.append(token)
                arg_count.append(0)
                if were_values:
                    were_values.pop()
                    were_values.append(True)
                were_values.append(False)
            elif token.ttype == "argument":
                while stack and stack[-1].tsubtype != "start":
                    output.append(self.create_node(stack.pop()))
                if not stack:
                    raise SyntaxError("Mismatched or misplaced parentheses")
                if were_values and were_values.pop():
                    arg_count[-1] += 1
                were_values.append(False)
            elif token.ttype.endswith("-prefix"):
                # Bugfix: prefix operators (e.g. u-) have no left operand and must never pop
                # pending operators off the stack (e.g. in '(A1,-B1)' union ',' was popped prematurely).
                # What was wrong: u- went through the same pop loop as infix operators.
                # How: infix pop logic compared precedence without distinguishing prefix operators.
                # Why: prefix operators wait for their operand and do not consume operators to their left.
                stack.append(token)
            elif token.ttype.endswith("-postfix"):
                # Bugfix: postfix % binds with Excel operator precedence (precedence 6).
                # What was wrong: % was previously rewritten as * 0.01 with precedence 4.
                # How: tokenizer emitted multiplication instead of postfix operator.
                # Why: postfix % binds tighter than ^ (5) and * / (4). We pop tighter operators, then push %.
                o1 = OPERATORS[token.tvalue]
                while stack and stack[-1].ttype.startswith("operator"):
                    o2 = OPERATORS["u-"] if (stack[-1].ttype.endswith("-prefix") and stack[-1].tvalue == "-") else OPERATORS[stack[-1].tvalue]
                    if (o1.associativity == "left" and o1.precedence <= o2.precedence) or (
                        o1.associativity == "right" and o1.precedence < o2.precedence
                    ):
                        output.append(self.create_node(stack.pop()))
                    else:
                        break
                stack.append(token)
            elif token.ttype.startswith("operator"):
                o1 = OPERATORS[token.tvalue]
                while stack and stack[-1].ttype.startswith("operator"):
                    o2 = OPERATORS["u-"] if (stack[-1].ttype.endswith("-prefix") and stack[-1].tvalue == "-") else OPERATORS[stack[-1].tvalue]
                    if (o1.associativity == "left" and o1.precedence <= o2.precedence) or (
                        o1.associativity == "right" and o1.precedence < o2.precedence
                    ):
                        output.append(self.create_node(stack.pop()))
                    else:
                        break
                stack.append(token)
            elif token.tsubtype == "start":
                stack.append(token)
            elif token.tsubtype == "stop":
                while stack and stack[-1].tsubtype != "start":
                    output.append(self.create_node(stack.pop()))
                if not stack:
                    raise SyntaxError("Mismatched or misplaced parentheses")
                stack.pop()
                if stack and stack[-1].ttype == "function":
                    func_node = self.create_node(stack.pop())
                    arg_n = arg_count.pop()
                    had_value = were_values.pop()
                    if had_value:
                        arg_n += 1
                    func_node.num_args = arg_n
                    output.append(func_node)

        while stack:
            if stack[-1].tsubtype in ("start", "stop"):
                raise SyntaxError("Mismatched or misplaced parentheses")
            output.append(self.create_node(stack.pop()))

        return list(output)

    def create_node(self, token) -> ast_nodes.ASTNode:
        if token.ttype == "operand":
            if token.tsubtype in ("range", "pointer"):
                return ast_nodes.RangeNode(token)
            return ast_nodes.OperandNode(token)
        if token.ttype == "function":
            return ast_nodes.FunctionNode(token)
        if token.ttype.startswith("operator"):
            return ast_nodes.OperatorNode(token)
        raise ValueError("Unknown token type: " + token.ttype)

    def build_ast(self, nodes: list) -> ast_nodes.ASTNode:
        stack: list = []
        for node in nodes:
            if isinstance(node, ast_nodes.OperatorNode):
                if node.ttype == "operator-infix":
                    if len(stack) < 2:
                        raise SyntaxError(f"Infix operator {node.tvalue} missing operands")
                    node.right = stack.pop()
                    node.left = stack.pop()
                elif node.ttype == "operator-postfix":
                    # Bugfix: postfix operators take the preceding operand on stack as left child.
                    if not stack:
                        raise SyntaxError(f"Postfix operator {node.tvalue} missing operand")
                    node.left = stack.pop()
                else:
                    if not stack:
                        raise SyntaxError(f"Prefix operator {node.tvalue} missing operand")
                    node.right = stack.pop()
            elif isinstance(node, ast_nodes.FunctionNode):
                if len(stack) < node.num_args:
                    raise SyntaxError(f"Function {node.tvalue} missing arguments in AST")
                args = [stack.pop() for _ in range(node.num_args)]
                node.args = list(reversed(args))
            stack.append(node)
        if len(stack) != 1:
            raise SyntaxError("Invalid formula structure: unconsumed nodes in stack")
        return stack.pop()
