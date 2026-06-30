# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""ppt-master UNO adapter layer (WriterAgent contrib). See README.md in this directory."""

from plugin.contrib.ppt_master.shape_ops import ShapeOp, SlideBuildPlan, slide_plan_to_dict, slide_plan_from_dict

__all__ = [
    "ShapeOp",
    "SlideBuildPlan",
    "slide_plan_to_dict",
    "slide_plan_from_dict",
]
