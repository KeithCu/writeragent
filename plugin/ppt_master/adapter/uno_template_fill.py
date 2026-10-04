import json
from pathlib import Path
from typing import Any, cast
from plugin.draw.bridge import DrawBridge
from plugin.draw.placeholders import _find_placeholder

def apply_fill_plan_to_doc(doc: Any, plan: dict[str, Any], *, template_doc: Any | None = None) -> dict[str, Any]:
    """Best-effort fill: duplicate slides and set placeholder/body text from plan."""
    slides = plan.get("slides")
    if not isinstance(slides, list) or not slides:
        return {"status": "error", "message": "fill_plan must contain a non-empty slides list."}

    bridge = DrawBridge(doc)
    filled = 0
    for offset, item in enumerate(slides):
        if not isinstance(item, dict):
            continue
        page, _idx = bridge.create_slide(offset, switch=False)
        item_dict = cast("dict[str, Any]", item)
        replacements = item_dict.get("replacements") or item_dict.get("text") or {}
        if isinstance(replacements, dict):
            for _key, text in replacements.items():
                if not text:
                    continue
                shape, shape_idx = _find_placeholder(page, _key)
                if shape is not None and hasattr(shape, "setString"):
                    shape.setString(str(text))
                    filled += 1
                else:
                    return {"status": "error", "message": f"Placeholder '{_key}' not found or unsupported on slide {offset}."}
        elif isinstance(replacements, str) and replacements.strip():
            if page.getCount() > 0:
                shape = page.getByIndex(0)
                if hasattr(shape, "setString"):
                    shape.setString(str(replacements).strip())
                    filled += 1
                else:
                    return {"status": "error", "message": f"No text shape available on slide {offset}."}
            else:
                return {"status": "error", "message": f"No shapes available on slide {offset}."}
    return {"status": "ok", "slides_created": len(slides), "fills_recorded": filled, "note": "Use export after SVG pipeline for full fidelity; template-fill UNO path is incremental."}

def apply_fill_plan_file(doc: Any, plan_path: Path) -> dict[str, Any]:
    data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    return apply_fill_plan_to_doc(doc, data)
