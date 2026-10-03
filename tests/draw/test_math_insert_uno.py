# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw math OLE size comes from the formula model's XVisualObject."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

from plugin.draw.math_insert import MATH_CLSID, _try_ole_shape_content_size_hmm, _visual_size_to_hundredth_mm


@native_test
@with_native_doc("impress")
def test_ole_content_size_reads_model_visual_area(ctx, doc):
    from com.sun.star.awt import Point, Size
    from com.sun.star.embed import Aspects

    page = doc.getDrawPages().getByIndex(0)
    shape = doc.createInstance("com.sun.star.drawing.OLE2Shape")
    page.add(shape)
    shape.setPosition(Point(1000, 1000))
    shape.setSize(Size(500, 400))
    shape.CLSID = MATH_CLSID
    # The bug: hasattr(shape, "getEmbeddedObject") is false on Draw OLE2Shape.
    assert hasattr(shape, "getEmbeddedObject") is False
    shape.Model.Formula = "a^2+b^2=c^2"
    aspect = int(getattr(Aspects, "MSOLE_CONTENT", 1))
    model = shape.Model
    expected = _visual_size_to_hundredth_mm(model.getVisualAreaSize(aspect), model.getMapUnit(aspect))
    assert expected is not None
    assert _try_ole_shape_content_size_hmm(shape) == expected
