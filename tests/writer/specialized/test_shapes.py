# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later


def test_writer_layout_tool_names():
    from plugin.writer.specialized import shapes as writer_shapes

    assert writer_shapes.AlignShapes.name == "shape_align"
    assert writer_shapes.DistributeShapes.name == "shape_distribute"
    assert writer_shapes.CreateDiagram.name == "shape_diagram"

