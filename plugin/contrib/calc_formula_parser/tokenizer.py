# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Portions Copyright (c) E. W. Bachtal, Robin Macharg, Bradley van Ree — xlcalculator (MIT)
# ========================================================================
#  Description: Tokenise an Excel formula using an implementation of
#               E. W. Bachtal's algorithm, found here:
#
#                   http://ewbi.blogs.com/develops/2004/12/excel_formula_p.html
#
#               Tested with Python v2.5 (win32)
#       Author: Robin Macharg
#    Copyright: Algorithm (c) E. W. Bachtal, this implementation (c) R. Macharg
#
#  Modification History
#
#  Date         Author Comment
#  =======================================================================
#  2006/11/29 - RMM  - Made strictly class-based.
#                      Added parse, render and pretty print methods
#  2006/11    - RMM  - RMM = Robin Macharg
#                            Created
#  2011/10    - Dirk Gorissen - Patch to support scientific notation
# ========================================================================

import re
from dataclasses import dataclass


# ========================================================================
#        Class: ExcelParserTokens
#  Description: Inheritable container for token definitions
#
#   Attributes: Self explanatory
#
#      Methods: None
# ========================================================================
class ExcelParserTokens(object):
    TOK_TYPE_NOOP = "noop"
    TOK_TYPE_OPERAND = "operand"
    TOK_TYPE_FUNCTION = "function"
    TOK_TYPE_SUBEXPR = "subexpression"
    TOK_TYPE_ARGUMENT = "argument"
    TOK_TYPE_OP_PRE = "operator-prefix"
    TOK_TYPE_OP_IN = "operator-infix"
    TOK_TYPE_OP_POST = "operator-postfix"
    TOK_TYPE_WSPACE = "white-space"
    TOK_TYPE_UNKNOWN = "unknown"

    TOK_SUBTYPE_START = "start"
    TOK_SUBTYPE_STOP = "stop"
    TOK_SUBTYPE_TEXT = "text"
    TOK_SUBTYPE_NUMBER = "number"
    TOK_SUBTYPE_LOGICAL = "logical"
    TOK_SUBTYPE_ERROR = "error"
    TOK_SUBTYPE_RANGE = "range"
    TOK_SUBTYPE_MATH = "math"
    TOK_SUBTYPE_CONCAT = "concatenate"
    TOK_SUBTYPE_INTERSECT = "intersect"
    TOK_SUBTYPE_UNION = "union"
    TOK_SUBTYPE_NONE = "none"


# ========================================================================
#        Class: f_token
#  Description: Encapsulate a formula token
#
#   Attributes:   tvalue -
#                  ttype - See token definitions, above, for values
#               tsubtype - See token definitions, above, for values
#
#      Methods: f_token  - __init__()
# ========================================================================
@dataclass(slots=True)
class f_token:

    tvalue: str
    ttype: str
    tsubtype: str

    def __repr__(self):
        return "<{} tvalue: {} ttype: {} tsubtype: {}>".format(
            self.__class__.__name__, self.tvalue, self.ttype, self.tsubtype)

    def __str__(self):
        return self.__repr__()


# ========================================================================
#        Class: f_tokens
#  Description: An ordered list of tokens
#
#   Attributes:        items - Ordered list
#                      index - Current position in the list
#
#      Methods: f_tokens     - __init__()
#               f_token      - add()      - Add a token to the end of the list
#               None         - addRef()   - Add a token to the end of the list
#               None         - reset()    - reset the index to -1
#               Boolean      - BOF()      - End of list?
#               Boolean      - EOF()      - Beginning of list?
#               Boolean      - moveNext() - Move the index along one
#               f_token/None - current()  - Return the current token
#               f_token/None - next()     - Return the next token (leave the
#                                           index unchanged)
#               f_token/None - previous() - Return the previous token (leave
#                                           the index unchanged)
# ========================================================================
class f_tokens(object):

    def __init__(self):
        self.items = []
        self.index = -1

    def add(self, value, type, subtype=""):
        if (not subtype):
            subtype = ""
        token = f_token(value, type, subtype)
        self.addRef(token)
        return token

    def addRef(self, token):
        self.items.append(token)

    def reset(self):
        self.index = -1

    def BOF(self):
        return self.index <= 0

    def EOF(self):
        return self.index >= (len(self.items) - 1)

    def moveNext(self):
        if self.EOF():
            return False
        self.index += 1
        return True

    def current(self):
        if self.index == -1:
            return None
        return self.items[self.index]

    def __next__(self):
        if self.EOF():
            raise StopIteration
        self.index += 1
        return self.items[self.index]

    def peek(self):
        if self.EOF():
            return None
        return self.items[self.index + 1]

    def previous(self):
        if self.index < 1:
            return None
        return self.items[self.index - 1]

    # Make this object pass as an iterator.
    def __iter__(self):
        self.reset()
        return self

    def next(self):
        return self.__next__()


# ========================================================================
#        Class: f_tokenStack
#     Inherits: ExcelParserTokens - a list of token values
#  Description: A LIFO stack of tokens
#
#   Attributes:        items - Ordered list
#
#      Methods: f_tokenStack - __init__()
#               None         - push(token) - Push a token onto the stack
#               f_token/None - pop()       - Pop a token off the stack
#               f_token/None - token()     - Non-destructively return the top
#                                            item on the stack
#               String       - type()      - Return the top token's type
#               String       - subtype()   - Return the top token's subtype
#               String       - value()     - Return the top token's value
# ========================================================================
class f_tokenStack(ExcelParserTokens):

    def __init__(self):
        self.items = []

    def push(self, token):
        self.items.append(token)

    def pop(self):
        if not self.items:
            raise SyntaxError("Unmatched closing parenthesis or bracket")
        token = self.items.pop()
        return f_token("", token.ttype, self.TOK_SUBTYPE_STOP)

    def token(self):
        return self.items[-1] if self.items else None

    def value(self):
        tok = self.token()
        return tok.tvalue if tok else ""

    def type(self):
        tok = self.token()
        return tok.ttype if tok else ""

    def subtype(self):
        tok = self.token()
        return tok.tsubtype if tok else ""


# ========================================================================
#        Class: ExcelParser
#  Description: Parse an Excel formula into a stream of tokens
#
#   Attributes:
#
#      Methods: f_tokens - getTokens(formula) - return a token stream (list)
# ========================================================================
class ExcelParser(ExcelParserTokens):

    def __init__(self, tokenize_range=False):
        if tokenize_range:
            self.OPERATORS = "+-*/^&=><:"
        else:
            self.OPERATORS = "+-*/^&=><"

    def getTokens(self, formula):

        def currentChar():
            return formula[offset] if offset < len(formula) else ""

        def doubleChar():
            return formula[offset:offset + 2]

        def nextChar():
            return formula[offset + 1] if offset + 1 < len(formula) else ""

        def EOF():
            return offset >= len(formula)

        tokens = f_tokens()
        tokenStack = f_tokenStack()
        offset = 0
        token = ""
        inString = False
        inPath = False
        inRange = False
        inError = False

        while len(formula) > 0 and formula[0] in (" ", "\n", "\t", "\r"):
            formula = formula[1:]
        if len(formula) > 0 and formula[0] == "=":
            formula = formula[1:]
        while len(formula) > 0 and formula[0] in (" ", "\n", "\t", "\r"):
            formula = formula[1:]

        # state-dependent character evaluation (order is important)
        while not EOF():
            # double-quoted strings
            # embeds are doubled
            # end marks token
            if inString:
                if currentChar() == "\"":
                    if nextChar() == "\"":
                        token += "\""
                        offset += 1

                    else:
                        inString = False
                        tokens.add(
                            token, self.TOK_TYPE_OPERAND,
                            self.TOK_SUBTYPE_TEXT)
                        token = ""

                else:
                    token += currentChar()
                offset += 1
                continue

            # single-quoted strings (sheet references / external paths)
            # embeds are double
            # end does not mark a token
            # Bugfix: keep quotes on quoted sheet names ('My Sheet'!A1) through tokenize/emit.
            # What was wrong: opening and closing single quotes were stripped from sheet names, and $'Sheet' emitted '$' as unknown.
            # How: inPath state did not append "'" to token, and non-empty token before "'" was emitted as TOK_TYPE_UNKNOWN.
            # Why: preserving quotes and allowing '$' prefix retains valid Calc/Excel sheet references.
            if inPath:
                if currentChar() == "'":
                    if nextChar() == "'":
                        token += "''"
                        offset += 2
                        continue
                    else:
                        inPath = False
                        token += "'"
                else:
                    token += currentChar()
                offset += 1
                continue

            # bracketed strings (range offset or linked workbook name)
            # no embeds (changed to "()" by Excel)
            # end does not mark a token
            if inRange:
                if currentChar() == "]":
                    inRange = False
                token += currentChar()
                offset += 1
                continue

            # error values
            # end marks a token, determined from absolute list of values
            if inError:
                token += currentChar()
                offset += 1
                if ",#NULL!,#DIV/0!,#VALUE!,#REF!,#NAME?,#NUM!,#N/A,".find(
                        "," + token + ",") != -1:
                    inError = False
                    tokens.add(
                        token, self.TOK_TYPE_OPERAND, self.TOK_SUBTYPE_ERROR)
                    token = ""
                elif currentChar() in (" ", "\n", "\t", "\r", ",", ")", "}", "+", "-", "*", "/", "^", "&", "=", "<", ">"):
                    inError = False
                    tokens.add(token, self.TOK_TYPE_OPERAND, self.TOK_SUBTYPE_ERROR)
                    token = ""
                continue

            # scientific notation check
            # Bugfix: support scientific notation with signed exponents (e.g. 0.5E+3, 10E+3, 1.E+3, 12e-3).
            # What was wrong: regex '^[1-9]{1}(\.[0-9]+)?[eE]{1}$' rejected leading zero mantissas (0.5E+3) or multi-digit (10E+3).
            # How: the mantissa pattern was strictly restricted to a single digit 1-9.
            # Why: matching any valid mantissa (digits with optional dot or starting with dot) followed by e/E keeps the signed exponent in the number token.
            regexSN = r'^([0-9]+(\.[0-9]*)?|\.[0-9]+)[eE]$'
            if currentChar() in "+-":
                if len(token) > 1 and re.match(regexSN, token, re.IGNORECASE):
                    token += currentChar()
                    offset += 1
                    continue

            # independent character evaulation (order not important)
            #
            # establish state-dependent character evaluations
            if currentChar() == "\"":
                if len(token) > 0:
                    # not expected
                    tokens.add(token, self.TOK_TYPE_UNKNOWN)
                    token = ""
                inString = True
                offset += 1
                continue

            if currentChar() == "'":
                if len(token) > 0 and token != "$":
                    # not expected
                    tokens.add(token, self.TOK_TYPE_UNKNOWN)
                    token = ""
                inPath = True
                token += "'"
                offset += 1
                continue

            if (currentChar() == "["):
                inRange = True
                token += currentChar()
                offset += 1
                continue

            if (currentChar() == "#"):
                if (len(token) > 0):
                    # not expected
                    tokens.add(token, self.TOK_TYPE_UNKNOWN)
                    token = ""
                inError = True
                token += currentChar()
                offset += 1
                continue

            # mark start and end of arrays and array rows
            if (currentChar() == "{"):
                if (len(token) > 0):
                    # not expected
                    tokens.add(token, self.TOK_TYPE_UNKNOWN)
                    token = ""
                tokenStack.push(tokens.add(
                    "ARRAY",
                    self.TOK_TYPE_FUNCTION, self.TOK_SUBTYPE_START))
                tokenStack.push(tokens.add(
                    "ARRAYROW",
                    self.TOK_TYPE_FUNCTION, self.TOK_SUBTYPE_START))
                offset += 1
                continue

            if (currentChar() == ";"):
                if (len(token) > 0):
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                tokens.addRef(tokenStack.pop())
                tokens.add(",", self.TOK_TYPE_ARGUMENT)
                tokenStack.push(tokens.add(
                    "ARRAYROW",
                    self.TOK_TYPE_FUNCTION, self.TOK_SUBTYPE_START))
                offset += 1
                continue

            if (currentChar() == "}"):
                if (len(token) > 0):
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                tokens.addRef(tokenStack.pop())
                tokens.addRef(tokenStack.pop())
                offset += 1
                continue

            # trim white-space
            # Bugfix: check not EOF() before currentChar() to avoid IndexError on trailing whitespace.
            # What was wrong: currentChar() ran before not EOF(), raising IndexError on trailing whitespace (e.g. '=A1 ').
            # How: formula[offset] indexed past the end of the string.
            # Why: checking not EOF() first and including \t/\r ensures clean loop exit at end of string.
            if currentChar() in (" ", "\n", "\t", "\r"):
                if len(token) > 0:
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                tokens.add("", self.TOK_TYPE_WSPACE)
                offset += 1
                while not EOF() and currentChar() in (" ", "\n", "\t", "\r"):
                    offset += 1
                continue

            # multi-character comparators
            if (",>=,<=,<>,".find("," + doubleChar() + ",") != -1):
                if (len(token) > 0):
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                tokens.add(
                    doubleChar(),
                    self.TOK_TYPE_OP_IN, self.TOK_SUBTYPE_LOGICAL)
                offset += 2
                continue

            # standard infix operators
            if (self.OPERATORS.find(currentChar()) != -1):
                if (len(token) > 0):
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                tokens.add(currentChar(), self.TOK_TYPE_OP_IN)
                offset += 1
                continue

            # standard postfix operators
            # Bugfix: Postfix % must bind with Excel postfix operator precedence (tighter than ^ and * /).
            # What was wrong: % was rewritten as '* 0.01' or evaluated as float(token)/100, which crashed on cell refs (A1%) and had wrong precedence.
            # How: % was not emitted as TOK_TYPE_OP_POST.
            # Why: Emitting TOK_TYPE_OP_POST allows parser shunting-yard to apply precedence 6.
            if currentChar() == "%":
                if len(token) > 0:
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                tokens.add(currentChar(), self.TOK_TYPE_OP_POST)
                offset += 1
                continue

            # start subexpression or function
            if (currentChar() == "("):
                if (len(token) > 0):
                    tokenStack.push(tokens.add(
                        token, self.TOK_TYPE_FUNCTION, self.TOK_SUBTYPE_START))
                    token = ""
                else:
                    tokenStack.push(tokens.add(
                        "", self.TOK_TYPE_SUBEXPR, self.TOK_SUBTYPE_START))
                offset += 1
                continue

            # function, subexpression, array parameters
            # Bugfix: empty function arguments (e.g. F(,1) or F(1,)) were miscounted because None operand was only emitted for ',,'.
            # What was wrong: F(,1) and F(1,) counted 1 argument instead of 2.
            # How: arguments before the first comma or after the last comma lacked an operand token.
            # Why: check if preceded by '(' or followed by ')' / ',' to emit a None operand.
            if currentChar() == ",":
                if len(token) > 0:
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                if tokenStack.type() != self.TOK_TYPE_FUNCTION:
                    tokens.add(currentChar(), self.TOK_TYPE_OP_IN, self.TOK_SUBTYPE_UNION)
                else:
                    prev = tokens.items[-1] if tokens.items else None
                    if prev and prev.ttype == self.TOK_TYPE_FUNCTION and prev.tsubtype == self.TOK_SUBTYPE_START:
                        tokens.add("None", self.TOK_TYPE_OPERAND, self.TOK_SUBTYPE_NONE)
                    tokens.add(currentChar(), self.TOK_TYPE_ARGUMENT)
                offset += 1
                # Bugfix: check not EOF() before currentChar() to avoid IndexError on trailing comma.
                if not EOF() and currentChar() == ",":
                    tokens.add("None", self.TOK_TYPE_OPERAND, self.TOK_SUBTYPE_NONE)
                    token = ""
                elif not EOF() and currentChar() == ")":
                    tokens.add("None", self.TOK_TYPE_OPERAND, self.TOK_SUBTYPE_NONE)
                    token = ""
                continue

            # stop subexpression
            if (currentChar() == ")"):
                if (len(token) > 0):
                    tokens.add(token, self.TOK_TYPE_OPERAND)
                    token = ""
                tokens.addRef(tokenStack.pop())
                offset += 1
                continue

            # token accumulation
            token += currentChar()
            offset += 1

        # dump remaining accumulation
        if (len(token) > 0):
            tokens.add(token, self.TOK_TYPE_OPERAND)

        # move all tokens to a new collection, excluding all unnecessary
        # white-space tokens
        tokens2 = f_tokens()

        while (tokens.moveNext()):
            token = tokens.current()

            if (token.ttype == self.TOK_TYPE_WSPACE):
                if ((tokens.BOF()) or (tokens.EOF())):
                    pass
                elif (not (
                     (
                         (tokens.previous().ttype == self.TOK_TYPE_FUNCTION)
                         and (tokens.previous().tsubtype
                              == self.TOK_SUBTYPE_STOP)
                     ) or (
                         (tokens.previous().ttype == self.TOK_TYPE_SUBEXPR)
                         and (tokens.previous().tsubtype
                              == self.TOK_SUBTYPE_STOP)
                     ) or (
                         tokens.previous().ttype == self.TOK_TYPE_OPERAND
                     )
                )):
                    pass
                elif (not (
                    (
                        (tokens.next().ttype == self.TOK_TYPE_FUNCTION)
                        and (tokens.next().tsubtype
                             == self.TOK_SUBTYPE_START)
                    ) or (
                        (tokens.next().ttype == self.TOK_TYPE_SUBEXPR)
                        and (tokens.next().tsubtype == self.TOK_SUBTYPE_START)
                    ) or (
                        tokens.next().ttype == self.TOK_TYPE_OPERAND)
                )):
                    pass
                else:
                    tokens2.add(
                        token.tvalue, self.TOK_TYPE_OP_IN,
                        self.TOK_SUBTYPE_INTERSECT)
                continue

            tokens2.addRef(token)

        # switch infix "-" operator to prefix when appropriate, switch infix
        # "+" operator to noop when appropriate, identify operand and
        # infix-operator subtypes, pull "@" from in front of function names
        while (tokens2.moveNext()):
            token = tokens2.current()
            if (
                    (token.ttype == self.TOK_TYPE_OP_IN)
                    and (token.tvalue == "-")
            ):
                if (tokens2.BOF()):
                    token.ttype = self.TOK_TYPE_OP_PRE
                elif (
                    (
                        (tokens2.previous().ttype == self.TOK_TYPE_FUNCTION)
                        and (tokens2.previous().tsubtype
                             == self.TOK_SUBTYPE_STOP)
                    ) or (
                        (tokens2.previous().ttype == self.TOK_TYPE_SUBEXPR)
                        and (tokens2.previous().tsubtype
                             == self.TOK_SUBTYPE_STOP)
                    ) or (
                        tokens2.previous().ttype == self.TOK_TYPE_OP_POST
                    ) or (
                        tokens2.previous().ttype == self.TOK_TYPE_OPERAND
                    )
                ):
                    token.tsubtype = self.TOK_SUBTYPE_MATH

                else:
                    token.ttype = self.TOK_TYPE_OP_PRE

                continue

            if (
                    (token.ttype == self.TOK_TYPE_OP_IN)
                    and (token.tvalue == "+")
            ):
                if tokens2.BOF():
                    token.ttype = self.TOK_TYPE_NOOP
                elif (
                    (
                        (tokens2.previous().ttype == self.TOK_TYPE_FUNCTION)
                        and (tokens2.previous().tsubtype
                             == self.TOK_SUBTYPE_STOP)
                    ) or (
                        (tokens2.previous().ttype == self.TOK_TYPE_SUBEXPR)
                        and (tokens2.previous().tsubtype
                             == self.TOK_SUBTYPE_STOP)
                    ) or (
                        tokens2.previous().ttype == self.TOK_TYPE_OP_POST
                    ) or (
                        tokens2.previous().ttype == self.TOK_TYPE_OPERAND
                    )
                ):
                    token.tsubtype = self.TOK_SUBTYPE_MATH

                else:
                    token.ttype = self.TOK_TYPE_NOOP

                continue

            if ((token.ttype == self.TOK_TYPE_OP_IN)
                    and (len(token.tsubtype) == 0)):
                if (("<>=").find(token.tvalue[0:1]) != -1):
                    token.tsubtype = self.TOK_SUBTYPE_LOGICAL

                elif (token.tvalue == "&"):
                    token.tsubtype = self.TOK_SUBTYPE_CONCAT

                else:
                    token.tsubtype = self.TOK_SUBTYPE_MATH

                continue

            if token.ttype == self.TOK_TYPE_OPERAND and len(token.tsubtype) == 0:
                str_val = str(token.tvalue).strip()
                is_num = False
                # Bugfix: exclude nan, inf, and identifiers with underscores from being misclassified as numbers.
                # What was wrong: float("nan") or float("inf") succeeded in Python 3, tagging range/identifier tokens as numbers.
                # How: bare float(token.tvalue) was called without checking for identifier characters or special floats.
                # Why: filtering out nan/inf and strings with '_' ensures valid Excel range/name identification.
                if str_val.lower() not in ("nan", "inf", "-inf", "+inf") and "_" not in str_val:
                    try:
                        float(str_val)
                        is_num = True
                    except ValueError:
                        pass
                if is_num:
                    token.tsubtype = self.TOK_SUBTYPE_NUMBER
                elif str_val.upper() in ("TRUE", "FALSE"):
                    # Bugfix: case-insensitive boolean literal classification
                    token.tsubtype = self.TOK_SUBTYPE_LOGICAL
                elif str_val == "None":
                    token.tsubtype = self.TOK_SUBTYPE_NONE
                else:
                    token.tsubtype = self.TOK_SUBTYPE_RANGE

                continue

            if (token.ttype == self.TOK_TYPE_FUNCTION):
                if (token.tvalue[0:1] == "@"):
                    token.tvalue = token.tvalue[1:]

                continue

        tokens2.reset()

        # move all tokens to a new collection, excluding all noops
        tokens = f_tokens()
        while (tokens2.moveNext()):
            if (tokens2.current().ttype != self.TOK_TYPE_NOOP):
                tokens.addRef(tokens2.current())

        tokens.reset()
        return tokens

    def parse(self, formula):
        self.tokens = self.getTokens(formula)
        return self.tokens
