with open("plugin/scripting/payload_codec.py", "r") as f:
    content = f.read()

import re

# Remove the decorators from _validate_split_grid_strings entirely
content = re.sub(
    r'@deal\.pre\(lambda envelope, \*\_\_unused, \*\*\_\_: \_is\_split\_grid\_envelope\(envelope\)\)\n@deal\.post\(lambda \*a, result=\_DEAL\_RETURN, \*\*k: isinstance\(\_deal\_return\(\*a, result=result\), list\)\)\n@deal\.raises\(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError\)\n\n\ndef \_validate\_split\_grid\_strings',
    'def _validate_split_grid_strings',
    content
)

content = re.sub(
    r'@deal\.pre\(lambda envelope, \*\_\_unused, \*\*\_\_: \_is\_split\_grid\_envelope\(envelope\)\)\n@deal\.post\(lambda \*a, result=\_DEAL\_RETURN, \*\*k: isinstance\(\_deal\_return\(\*a, result=result\), list\)\)\n@deal\.raises\(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError\)\ndef \_validate\_split\_grid\_strings',
    'def _validate_split_grid_strings',
    content
)

# And make sure they are on host_unpack_split_grid
content = re.sub(
    r'def host_unpack_split_grid\(',
    '@deal.pre(lambda envelope, *_unused, **__: _is_split_grid_envelope(envelope))\n@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), list))\n@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)\ndef host_unpack_split_grid(',
    content
)

# wait, I did a replace earlier that might have put it twice.
# Let's just remove ALL @deal.post for _validate_split_grid_strings manually

# Actually, the error says:
# deal.PostContractError: expected isinstance(_deal_return(*a, result=result), list)
# And the traceback points to:
# File "/app/plugin/scripting/payload_codec.py", line 1595, in child_unpack_split_grid
#    strings = _validate_split_grid_strings(envelope, expected_cells_check)
# This proves _validate_split_grid_strings STILL has the decorators! Because _validate_split_grid_strings returns a dict, not a list!
