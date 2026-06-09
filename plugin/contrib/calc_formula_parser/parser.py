# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Portions Copyright (c) Bradley van Ree — xlcalculator (MIT); see README.md
"""Shunting-yard formula parser → AST."""

from __future__ import annotations

from plugin.contrib.calc_formula_parser import ast_nodes, tokenizer


class Operator:
    def __init__(self, value: str, precedence: int, associativity: str) -> None:
        self.value = value
        self.precedence = precedence
        self.associativity = associativity


# Excel operator precedence (Microsoft docs).
OPERATORS = {
    ":": Operator(":", 8, "left"),
    "": Operator(" ", 8, "left"),
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
            for index, token in enumerate(tokens):
                new_tokens.append(token)
                if not isinstance(token.tvalue, str):
                    continue
                if token.tvalue.startswith(":"):
                    depth = 0
                    expr = ""
                    rev = reversed(tokens[:index])
                    for reversed_token in rev:
                        if reversed_token.tsubtype == "stop":
                            depth += 1
                        elif depth > 0 and reversed_token.tsubtype == "start":
                            depth -= 1
                        expr = reversed_token.tvalue + expr
                        new_tokens.pop()
                        if depth == 0:
                            new_tokens.pop()
                            new_tokens.pop()
                            expr = rev.__next__().tvalue + expr
                            break
                    expr += token.tvalue
                    depth = 0
                    if token.tvalue[1:] in ("OFFSET", "INDEX"):
                        for t in tokens[(index + 1) :]:
                            if t.tsubtype == "start":
                                depth += 1
                            elif depth > 0 and t.tsubtype == "stop":
                                depth -= 1
                            expr += t.tvalue
                            tokens.remove(t)
                            if depth == 0:
                                break
                    new_tokens.append(tokenizer.f_token(expr, "operand", "pointer"))
                elif ":OFFSET" in token.tvalue or ":INDEX" in token.tvalue:
                    depth = 0
                    expr = token.tvalue
                    for t in tokens[(index + 1) :]:
                        if t.tsubtype == "start":
                            depth += 1
                        elif t.tsubtype == "stop":
                            depth -= 1
                        expr += t.tvalue
                        tokens.remove(t)
                        if depth == 0:
                            new_tokens.pop()
                            break
                    new_tokens.append(tokenizer.f_token(expr, "operand", "pointer"))

        tokens = new_tokens if new_tokens else tokens

        for token in tokens:
            if token.ttype == "operand":
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
                if were_values.pop():
                    arg_count[-1] += 1
                were_values.append(False)
                if not stack:
                    raise ValueError("Mismatched or misplaced parentheses")
            elif token.ttype.startswith("operator"):
                if token.ttype.endswith("-prefix") and token.tvalue == "-":
                    o1 = OPERATORS["u-"]
                else:
                    o1 = OPERATORS[token.tvalue]
                while stack and stack[-1].ttype.startswith("operator"):
                    if stack[-1].ttype.endswith("-prefix") and stack[-1].tvalue == "-":
                        o2 = OPERATORS["u-"]
                    else:
                        o2 = OPERATORS[stack[-1].tvalue]
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
                    node.right = stack.pop()
                    node.left = stack.pop()
                else:
                    node.right = stack.pop()
            elif isinstance(node, ast_nodes.FunctionNode):
                args = [stack.pop() for _ in range(node.num_args)]
                node.args = list(reversed(args))
            stack.append(node)
        return stack.pop()
