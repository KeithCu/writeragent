with open("plugin/scripting/payload_codec.py", "r") as f:
    content = f.read()

import re

# We need to carefully remove the decorators from _validate_split_grid_strings, and ONLY from there.

old_str = """@deal.pre(lambda envelope, *_unused, **__: _is_split_grid_envelope(envelope))
@deal.post(lambda *a, result=_DEAL_RETURN, **k: isinstance(_deal_return(*a, result=result), list))
@deal.raises(ValueError, TypeError, AttributeError, OverflowError, RecursionError, IndexError, KeyError)
def _validate_split_grid_strings"""

new_str = """def _validate_split_grid_strings"""

content = content.replace(old_str, new_str)

with open("plugin/scripting/payload_codec.py", "w") as f:
    f.write(content)
