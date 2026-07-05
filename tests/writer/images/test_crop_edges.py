# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""T3 (R3): set_image_properties really crops now (GraphicCrop). Pin the mm -> 1/100mm edge math
and the preserve-unspecified-edges behavior. No LibreOffice required. Applying the struct is live."""
from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.images.images import _resolve_crop_edges


def test_only_top_overrides_others_preserved():
    # current crop = (top, bottom, left, right) in 1/100mm
    out = _resolve_crop_edges({"crop_top_mm": 5}, (0, 200, 300, 400))
    assert out == (500, 200, 300, 400)


def test_all_edges_mm_to_hundredths():
    out = _resolve_crop_edges(
        {"crop_top_mm": 1, "crop_bottom_mm": 2, "crop_left_mm": 3, "crop_right_mm": 4.5}, (0, 0, 0, 0)
    )
    assert out == (100, 200, 300, 450)


def test_none_keeps_current():
    assert _resolve_crop_edges({}, (10, 20, 30, 40)) == (10, 20, 30, 40)


def test_rounding():
    assert _resolve_crop_edges({"crop_left_mm": 1.234}, (0, 0, 0, 0)) == (0, 0, 123, 0)
