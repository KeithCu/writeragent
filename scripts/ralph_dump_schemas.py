"""Dump every concrete tool's name, description and parameters (per doc type) as sorted JSON.
Usage: .venv/bin/python scripts/ralph_dump_schemas.py > /tmp/wa-schemas-before.json   (then after; diff must be empty)
Key order inside each schema is preserved (json.dumps without sort_keys) so order changes show up."""
import importlib
import json
import pkgutil
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))  # run from repo root
import tests.conftest  # noqa: F401  installs the uno mocks used by unit tests
import plugin
from plugin.framework.tool import ToolBase
for m in pkgutil.walk_packages(plugin.__path__, "plugin."):
    if any(x in m.name for x in (".lib", ".contrib", "embeddings", "ppt_master", "writeragent_api", "venv", ".tests")): continue
    try: importlib.import_module(m.name)
    except BaseException as e: print(f"# import failed {m.name}: {type(e).__name__}", file=sys.stderr)
seen, stack, out = set(), list(ToolBase.__subclasses__()), {}
while stack:
    c = stack.pop()
    if c in seen: continue
    seen.add(c)
    stack.extend(c.__subclasses__())
    if not (isinstance(getattr(c, "name", None), str) and c.name) or c.__module__.startswith("tests"): continue
    try: inst = c()
    except Exception as e:
        out[f"{c.__module__}.{c.__qualname__}"] = f"<init failed {type(e).__name__}>"
        continue
    out[f"{c.__module__}.{c.__qualname__}"] = {dt: [inst.get_parameters(dt), str(getattr(inst, "description", ""))] for dt in (None, "writer", "calc", "draw")}
for k in sorted(out): print(k, json.dumps(out[k], ensure_ascii=False, default=str))
