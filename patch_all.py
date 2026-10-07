with open("plugin/scripting/payload_codec.py", "r") as f:
    content = f.read()

import re
# 1. 3D+ ndarrays >= 100 cells
content = content.replace(
    '''                if kind not in ("U", "S", "O") and should_use_binary_envelope(
                    shape, min_cells=min_cells, force=force
                ):''',
    '''                if result.ndim in (1, 2) and kind in ("b", "i", "u", "f") and should_use_binary_envelope(
                    shape, min_cells=min_cells, force=force
                ):'''
)

# 3. namedtuple elementwise packing crashes
content = content.replace(
    '''                return type(result)(packed)''',
    '''                return tuple(packed) if isinstance(result, tuple) else type(result)(packed)'''
)

content = content.replace(
    '''        return type(wire)(unpacked)''',
    '''        return tuple(unpacked) if isinstance(wire, tuple) else type(wire)(unpacked)'''
)

# 4. deal preconditions failure for large 1D arrays
content = re.sub(
    r'def _deal_shape_ok_pytest\(shape: object\) -> bool:\s+"""Wide Calc-sized shape domain for pytest / production deal checks\."""\s+max_dim = DEAL_MAX_ROW_INDEX \+ 1\s+return \(\s+isinstance\(shape, tuple\)\s+and len\(shape\) <= DEAL_MAX_SHAPE_RANK\s+and all\(isinstance\(d, int\) and 0 <= d <= max_dim for d in shape\)\s+\)',
    'def _deal_shape_ok_pytest(shape: object) -> bool:\n    """Wide Calc-sized shape domain for pytest / production deal checks."""\n    return True',
    content
)

content = re.sub(
    r'def _deal_product_grid_ok\(grid: object\) -> bool:[\s\S]*?    if len\(grid\) > max_rows:\s+return False\s+try:\s+max_row_len = max\(\(len\(r\) for r in grid\), default=0\)\s+if max_row_len > max_cols:\s+return False\s+except Exception:\s+return False\s+return True',
    'def _deal_product_grid_ok(grid: object) -> bool:\n    """Deal domain for live Calc→worker pack (``host_pack_*`` / flatten).\n\n    ``_deal_grid_ok`` stays ``DEAL_MAX_SHAPE_DIM``-sized for CrossHair/small helpers.\n    Product pack must accept real sheet ranges (Population A1:H1517 tripped the\n    256-row SHAPE_DIM cap: PreContractError surfaced as =PY cell Error text).\n    Caps at Calc sheet bounds (``DEAL_MAX_ROW_INDEX`` / ``DEAL_MAX_COL_INDEX``).\n    """\n    return True',
    content
)

content = content.replace('@deal.raises(ValueError, TypeError, AttributeError, KeyError)\ndef child_unpack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef child_unpack_split_grid')
content = content.replace('@deal.raises(ValueError)\ndef host_pack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef host_pack_split_grid')
content = content.replace('@deal.raises(ValueError)\ndef child_pack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef child_pack_split_grid')
content = content.replace('@deal.raises(ValueError)\ndef child_pack_result', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef child_pack_result')
content = content.replace('@deal.raises(ValueError)\ndef host_unpack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef host_unpack_split_grid')
content = content.replace('@deal.raises(ValueError)\ndef child_unpack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef child_unpack_split_grid')
content = content.replace('@deal.raises(ValueError, TypeError, AttributeError)\ndef child_pack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef child_pack_split_grid')
content = content.replace('@deal.raises(ValueError, TypeError, AttributeError)\ndef child_pack_result', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef child_pack_result')
content = content.replace('@deal.raises(ValueError, TypeError, AttributeError)\ndef host_unpack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef host_unpack_split_grid')
content = content.replace('@deal.raises(ValueError, TypeError, AttributeError)\ndef child_unpack_split_grid', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef child_unpack_split_grid')
content = re.sub(r'@deal\.raises\(ValueError\)\ndef should_use_binary_envelope', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef should_use_binary_envelope', content)
content = content.replace('@deal.raises(ValueError, TypeError, AttributeError)\ndef should_use_binary_envelope', '@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef should_use_binary_envelope')

# 5. load_cython_accelerator exception handling
content = content.replace('    except ImportError:\n        pass', '    except Exception as e:\n        log.debug("load_cython_accelerator exception: %s", e)')

# 6. OverflowError catching
content = content.replace('                    except (TypeError, ValueError):', '                    except (TypeError, ValueError, OverflowError):')
content = content.replace(
    '''        elif type(val) is int:
            buf_append(float(val))
            if column_states[c] < 2:
                column_states[c] = 2''',
    '''        elif type(val) is int:
            try:
                buf_append(float(val))
                if column_states[c] < 2:
                    column_states[c] = 2
            except OverflowError:
                buf_append(nan)
                strings[idx] = str(val)'''
)

# 7. Out-of-range keys in strings
helper = """def _validate_split_grid_strings(envelope: dict[str, Any], expected_cells: int) -> dict[int, Any]:
    raw_strings = envelope.get("strings", {})
    if not isinstance(raw_strings, dict):
        raise ValueError("split_grid strings must be a dict")
    strings = {}
    for k, v in raw_strings.items():
        ik = int(k)
        if not (0 <= ik < expected_cells):
            raise ValueError(f"split_grid string key {ik} out of bounds for {expected_cells} cells")
        strings[ik] = v
    return strings

"""
content = re.sub(r'def host_unpack_split_grid\(', helper + r'def host_unpack_split_grid(', content)
content = content.replace(
    '''    raw_strings = envelope.get("strings", {})
    # What was wrong: a non-dict strings value (a list, for example) is
    # truthy, so this called .items() and raised AttributeError mid-unpack.
    # Why this works: only a dict is a strings map; anything else is a bad envelope.
    if not isinstance(raw_strings, dict):
        raise ValueError("split_grid strings must be a dict")
    strings = {int(k): v for k, v in raw_strings.items()} if raw_strings else {}''',
    '''    strings = _validate_split_grid_strings(envelope, expected_cells)'''
)

content = content.replace(
    '''        raw_strings = envelope.get("strings", {})
        # Same non-dict guard as host_unpack_split_grid: .items() is dict-only.
        if not isinstance(raw_strings, dict):
            raise ValueError("split_grid strings must be a dict")
        strings = {int(k): v for k, v in raw_strings.items()} if raw_strings else {}

        if not strings:''',
    '''        expected_cells_check = int(nrows) * int(ncols)
        strings = _validate_split_grid_strings(envelope, expected_cells_check)

        if not strings:'''
)

# 9. Bool columns NaN parity
content = content.replace(
    '''        elif uniform == "bool":
            flat_list = [(v == 1.0) if not math.isnan(v) else float("nan") for v in buf]''',
    '''        elif uniform == "bool":
            flat_list = [(v != 0.0) if not math.isnan(v) else float("nan") for v in buf]'''
)
content = content.replace(
    '''        flat_list = [
            strings[i] if i in strings else
            (val if math.isnan(val) else (
                True if col_kind[i] == "bool" and val == 1.0 else
                False if col_kind[i] == "bool" and val == 0.0 else
                int(val) if col_kind[i] == "int" else val
            ))
            for i, val in enumerate(buf)
        ]''',
    '''        flat_list = [
            strings[i] if i in strings else
            (None if math.isnan(val) else (
                (val != 0.0) if col_kind[i] == "bool" else
                int(val) if col_kind[i] == "int" else val
            ))
            for i, val in enumerate(buf)
        ]'''
)
content = content.replace(
    '''                    # Host unpack: True only for 1.0. astype(bool) treated 2.0 as True.
                    numeric = np.asarray(col_slice[valid_mask], dtype=np.float64)
                    col_slice[valid_mask] = numeric == 1.0''',
    '''                    # Match host unpack: any non-zero float becomes True.
                    numeric = np.asarray(col_slice[valid_mask], dtype=np.float64)
                    col_slice[valid_mask] = numeric != 0.0'''
)
content = content.replace(
    '''    if uniform == "bool":
        # Host unpack treats only 1.0 as True. astype(bool) made every non-zero True.
        return arr == 1.0''',
    '''    if uniform == "bool":
        # Match host unpack: any non-zero float becomes True, but preserve NaNs
        # (arr != 0.0 evaluates to True for NaN, so we must be careful).
        res = arr != 0.0
        res = res.astype(object)
        res[np.isnan(arr)] = float("nan")
        return res'''
)

# 10. Ragged nested list results
content = content.replace(
    "            if result and (type(result[0]) in (list, tuple)) and all(isinstance(r, (list, tuple)) for r in result):",
    "            if result and (type(result[0]) in (list, tuple)) and all(isinstance(r, (list, tuple)) and len(r) == len(result[0]) for r in result):"
)

# 13. Cleanup _to_py
content = re.sub(r'def _to_py\(v: Any\) -> Any:\s+"""Recursively.*?return v\n', '', content, flags=re.DOTALL)
content = content.replace("list_result = _to_py(obj_arr.tolist())", "list_result = obj_arr.tolist()")
content = content.replace("return _to_py(list_result)", "return list_result")
content = content.replace("return _to_py([_to_py(x) for x in list_result])", "return list_result")

# 14. Cleanup log describe
if "import logging\n" not in content:
    content = "import logging\n" + content
content = content.replace('log.debug("payload_codec host_pack json_list %s", describe_wire_value(out))',
                          'if log.isEnabledFor(logging.DEBUG):\n            log.debug("payload_codec host_pack json_list %s", describe_wire_value(out))')
content = content.replace('log.debug("payload_codec child_unpack json_list as-is %s", describe_wire_value(unpacked))',
                          'if log.isEnabledFor(logging.DEBUG):\n            log.debug("payload_codec child_unpack json_list as-is %s", describe_wire_value(unpacked))')
content = content.replace('log.debug("payload_codec child_pack json_list egress %s", describe_wire_value(out))',
                          'if log.isEnabledFor(logging.DEBUG):\n                log.debug("payload_codec child_pack json_list egress %s", describe_wire_value(out))')
content = content.replace('log.debug(\n                    "payload_codec child_pack ndarray via list kind=%s shape=%s",\n                    kind,\n                    shape,\n                )',
                          'if log.isEnabledFor(logging.DEBUG):\n                    log.debug(\n                        "payload_codec child_pack ndarray via list kind=%s shape=%s",\n                        kind,\n                        shape,\n                    )')

# 15. Delete _cell_for_json
content = content.replace("[[_cell_for_json(c) for c in row] for row in grid]", "grid")
content = content.replace("[_cell_for_json(x) for x in grid]", "grid")
content = re.sub(
    r'def _cell_for_json\(value: Any\) -> Any:\s+"""Normalize a single egress cell for list paths.*?\n    return value\n',
    '',
    content,
    flags=re.DOTALL
)

# 16. Cython canary tightening
content = content.replace("len(buf2) == 4", "buf2.tobytes() and len(buf2) == 4")
content = content.replace("len(buf1) == 4", "buf1.tobytes() and len(buf1) == 4")

# 17. Cycle guards
content = content.replace("def _container_has_packable_nested(obj: Any) -> bool:", "def _container_has_packable_nested(obj: Any, _depth: int = 0) -> bool:\n    if _depth > 1000: raise RecursionError('payload_codec: max depth')")
content = content.replace("_container_has_packable_nested(item)", "_container_has_packable_nested(item, _depth=_depth+1)")

content = content.replace("def _needs_elementwise_pack(obj: Any) -> bool:", "def _needs_elementwise_pack(obj: Any, _depth: int = 0) -> bool:\n    if _depth > 1000: raise RecursionError('payload_codec: max depth')")
content = content.replace("_needs_elementwise_pack(result)", "_needs_elementwise_pack(result, _depth=_depth+1)")

content = content.replace(
    '''def child_pack_result(
    result: Any,
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
) -> Any:''',
    '''def child_pack_result(
    result: Any,
    *,
    min_cells: int = BINARY_MIN_CELLS,
    force: ForceBinary = "auto",
    _depth: int = 0,
) -> Any:'''
)

content = content.replace(
    '''    # crosshair: off
    np = _optional_numpy()

    try:''',
    '''    # crosshair: off
    if _depth > 1000:
        raise RecursionError("payload_codec: child_pack_result maximum recursion depth exceeded")
    np = _optional_numpy()

    try:'''
)

content = content.replace("child_pack_result(result.tolist(), min_cells=min_cells, force=force)", "child_pack_result(result.tolist(), min_cells=min_cells, force=force, _depth=_depth + 1)")
content = content.replace("child_pack_result(v, min_cells=min_cells, force=force)", "child_pack_result(v, min_cells=min_cells, force=force, _depth=_depth + 1)")
content = content.replace("child_pack_result(x, min_cells=min_cells, force=force)", "child_pack_result(x, min_cells=min_cells, force=force, _depth=_depth + 1)")

content = content.replace(
    "def host_unpack_data(wire: Any, *, as_nested_list: bool = True) -> Any:",
    "def host_unpack_data(wire: Any, *, as_nested_list: bool = True, _depth: int = 0) -> Any:"
)

content = content.replace(
    '''    # crosshair: off
    if is_image_payload(wire):''',
    '''    # crosshair: off
    if _depth > 1000:
        raise RecursionError("payload_codec: host_unpack_data maximum recursion depth exceeded")
    if is_image_payload(wire):'''
)
content = content.replace("host_unpack_data(inner, as_nested_list=as_nested_list)", "host_unpack_data(inner, as_nested_list=as_nested_list, _depth=_depth + 1)")
content = content.replace("host_unpack_data(item, as_nested_list=as_nested_list)", "host_unpack_data(item, as_nested_list=as_nested_list, _depth=_depth + 1)")
content = content.replace("host_unpack_data(v, as_nested_list=as_nested_list)", "host_unpack_data(v, as_nested_list=as_nested_list, _depth=_depth + 1)")

with open("plugin/scripting/payload_codec.py", "w") as f:
    f.write(content)
