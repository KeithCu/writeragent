# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract sidecar for plugin.scripting.audio_silence_detector."""

from __future__ import annotations

import deal

from plugin.scripting import audio_silence_detector as _prod
from tests.contracts.bounds import DEAL_MAX_ARGV, UNDER_CROSSHAIR


def _deal_pcm_ok_pytest(pcm: object) -> bool:
    return isinstance(pcm, (bytes, bytearray))


def _deal_pcm_ok_crosshair(pcm: object) -> bool:
    return isinstance(pcm, (bytes, bytearray)) and len(pcm) <= DEAL_MAX_ARGV


_deal_pcm_ok = _deal_pcm_ok_crosshair if UNDER_CROSSHAIR else _deal_pcm_ok_pytest

pcm_energy_int16 = deal.post(
    lambda result: isinstance(result, tuple)
    and len(result) == 2
    and 0.0 <= result[0] <= 1.0
    and 0.0 <= result[1] <= 1.0
)(
    deal.pre(lambda pcm: _deal_pcm_ok(pcm))(_prod.pcm_energy_int16)
)

PYTEST = ["pcm_energy_int16"]
TARGETS: list[str] = []
