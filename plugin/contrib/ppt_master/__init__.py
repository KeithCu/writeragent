# WriterAgent - vendored ppt-master conversion helpers (LGPL/GPL per upstream + WriterAgent headers on new files).
"""Bundled ppt-master UNO adapter layer (no upstream svg_to_pptx copy).

UPSTREAM NOTE (WriterAgent addition — not in upstream):
  Unmodified ppt-master Python and assets load from the user venv pip install
  (``skills/ppt-master``). This package only contains WriterAgent-specific adapters.
  See README.md in this directory.
"""

from plugin.contrib.ppt_master.shape_ops import ShapeOp, SlideBuildPlan, slide_plan_to_dict, slide_plan_from_dict

__all__ = [
    "ShapeOp",
    "SlideBuildPlan",
    "slide_plan_to_dict",
    "slide_plan_from_dict",
]
