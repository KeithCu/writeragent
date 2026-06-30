# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from plugin.contrib.ppt_master.coords import parse_viewbox, px_to_hmm, slide_dims_for_viewbox


def test_parse_viewbox_ppt169():
    _min_x, _min_y, w, h = parse_viewbox("0 0 1280 720")
    assert w == 1280.0
    assert h == 720.0


def test_px_to_hmm_center():
    hmm = px_to_hmm(640, slide_hmm=25400, viewbox_px=1280)
    assert hmm == 12700


def test_slide_dims_for_viewbox():
    w, h = slide_dims_for_viewbox(1280, 720, width_hmm=25400)
    assert w == 25400
    assert h == 14288
