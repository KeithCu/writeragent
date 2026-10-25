"""UNO component modules must not define dataclasses at import time.

LibreOffice's pythonloader execs a component file into a module that is not in
``sys.modules``. ``@dataclass`` then fails with ``'NoneType' object has no
attribute '__dict__'`` and ``unopkg add`` aborts.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _component_modules() -> list[Path]:
    return [p for p in sorted((ROOT / "plugin").rglob("*.py")) if "\ng_ImplementationHelper" in p.read_text(encoding="utf-8")]


def _uses_dataclass(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name == "dataclass":
                    return True
    return False


def test_component_modules_found() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in _component_modules()}
    assert "plugin/chatbot/panel_factory.py" in names
    assert "plugin/main.py" in names


def test_component_modules_define_no_dataclass() -> None:
    bad = [p.relative_to(ROOT).as_posix() for p in _component_modules() if _uses_dataclass(ast.parse(p.read_text(encoding="utf-8")))]
    assert bad == []
