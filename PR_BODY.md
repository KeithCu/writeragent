Tighten CrossHair cover-all WRITERAGENT_CROSSHAIR=1 deal.pre domains to help it finish under 6 hours on GitHub-hosted runners.

Modifications:
- `plugin/calc/excel_py_convert/to_dag.py`: Added bounds in `ast_source_offset` (`@deal.pre(lambda src, lineno, col: ascii_bounded(src, DEAL_MAX_SOURCE) and type(lineno) is int and type(col) is int)`). Moved `len(result) == len(src)` in `_normalize_excel_placeholders` to `inverse_ensure`, added `@deal.pre(lambda src: str_bounded(src, DEAL_MAX_SOURCE))`.
- `plugin/scripting/calc_range.py`: Added length bounds to `_materialize_inner_grid` and `_is_json_list_of_grids` (`<= DEAL_MAX_SHAPE_DIM`). Added type bound to `CalcRange` rich-compare dunders (`__eq__`, `__ne__`, `__lt__`, `__le__`, `__gt__`, `__ge__`) (`@deal.pre(lambda self, other: type(other) is int)`).
- `plugin/framework/appearance.py`: Bound `_darken` (`@deal.pre(lambda color, factor: type(color) is int and 0 <= color <= 0xFFFFFF and type(factor) is float and 0.0 <= factor <= 1.0)`).
- `plugin/embeddings/embeddings_split.py`: Added string bounds in `_split_passage_whitespace_to_sentences` (`str_bounded(..., DEAL_MAX_SOURCE)`). Bounded length in `_sentences_spans_ok` and `split_passage_to_chunk_meta`.
- `plugin/framework/json_utils.py`: Bounded `repair_json` and `repair_json_object` with `str_bounded(..., DEAL_MAX_SOURCE)`.
- `plugin/chatbot/tool_loop_state.py`: Bound `domain_from_delegate_args` and `_describe_empty_response_tool_calls` (`len(...) <= DEAL_MAX_SHAPE_DIM`).
- `plugin/framework/client/auth.py`: Bound `_resolve_provider_id` with `ascii_bounded(..., DEAL_MAX_TOKEN)`.
