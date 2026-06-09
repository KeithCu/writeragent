# calc_formula_parser

Vendored parse-only slice from [xlcalculator](https://github.com/bradbase/xlcalculator) (MIT).

- `tokenizer.py` — E. W. Bachtal / Robin Macharg Excel formula tokenizer
- `parser.py` — shunting-yard → AST
- `ast_nodes.py` — slim AST nodes (eval removed; codegen-only)

Used by `plugin/calc/spreadsheet_import/translate.py` for Calc → `=PY()` conversion.
