# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""RMS-based end-of-speech detection for sidebar microphone capture (venv and host paths)."""

from __future__ import annotations

import logging
import math
import struct
from collections import deque
from dataclasses import dataclass

from plugin.framework.deal_shim import DEAL_MAX_ARGV, UNDER_CROSSHAIR, deal


def _deal_pcm_ok_pytest(pcm: object) -> bool:
    # A mic buffer is larger than DEAL_MAX_ARGV bytes. The cap raised
    # PreContractError before RMS ran. The body walks every sample.
    return isinstance(pcm, (bytes, bytearray))


def _deal_pcm_ok_crosshair(pcm: object) -> bool:
    return isinstance(pcm, (bytes, bytearray)) and len(pcm) <= DEAL_MAX_ARGV


_deal_pcm_ok = _deal_pcm_ok_crosshair if UNDER_CROSSHAIR else _deal_pcm_ok_pytest

log = logging.getLogger(__name__)

# Algorithm tuning (not user config — adjust here if mic edge cases appear).
MIN_SPEECH_MS = 500
SPEECH_ONSET_MS = 200
MIN_SILENCE_FLOOR = 0.003
LOUD_ONSET_RMS = 0.03
SPEECH_HYSTERESIS_FACTOR = 1.8
PEAK_SILENCE_FACTOR = 1.5
NOISE_FLOOR_MULTIPLIER = 1.5
NOISE_EMA_TAU_SEC = 0.25
# Flat sub-loud energy of this length is room noise, not a held speech run.
# It stays under MIN_SPEECH_MS so the stretch cannot arm auto-stop by itself.
STEADY_NOISE_MS = 400
DEFAULT_SILENCE_STOP_MS = 3000


@dataclass(frozen=True)
class SilenceDetectorConfig:
    """User-facing silence auto-stop (see chatbot.audio_silence_stop_ms in module.yaml)."""

    silence_stop_ms: int = DEFAULT_SILENCE_STOP_MS

    @property
    def enabled(self) -> bool:
        return self.silence_stop_ms > 0


@dataclass(frozen=True)
class SilenceDetectorResult:
    """One chunk's classification plus the running speech and silence counters."""

    rms: float
    peak: float
    is_speech: bool
    silence_ms: int
    speech_ms: int
    should_stop: bool
    heard_speech: bool


@deal.pre(lambda pcm: _deal_pcm_ok(pcm))
@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and 0.0 <= result[0] <= 1.0 and 0.0 <= result[1] <= 1.0)
def pcm_energy_int16(pcm: bytes) -> tuple[float, float]:
    """Return (RMS, peak) of 16-bit little-endian PCM, each normalized to 0.0–1.0."""
    # crosshair: off
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return 0.0, 0.0
    samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])
    sum_sq = sum(sample * sample for sample in samples)
    peak = max(abs(sample) for sample in samples)
    rms = (sum_sq / sample_count) ** 0.5
    return min(1.0, rms / 32768.0), min(1.0, peak / 32768.0)


def _resolve_silence_stop_ms() -> int:
    """Read chatbot.audio_silence_stop_ms (0 = manual Stop Rec only)."""
    from plugin.framework.config import get_config_dict

    raw = get_config_dict()
    if "chatbot.audio_silence_stop_ms" not in raw:
        return DEFAULT_SILENCE_STOP_MS
    try:
        return max(0, int(raw["chatbot.audio_silence_stop_ms"]))
    except (TypeError, ValueError):
        return DEFAULT_SILENCE_STOP_MS


def load_silence_detector_config() -> SilenceDetectorConfig:
    """Build detector settings from writeragent.json."""
    stop_ms = _resolve_silence_stop_ms()
    if stop_ms == 0:
        log.info("audio VAD: auto-stop disabled (silence_stop_ms=0)")
    return SilenceDetectorConfig(silence_stop_ms=stop_ms)


class SilenceDetector:
    """Track consecutive silence after minimum speech; trigger auto-stop without STT."""

    _config: SilenceDetectorConfig
    _sample_rate: int
    _noise_ema: float | None
    _run_samples: int
    _run_committed: bool
    _speech_samples: int
    _silence_samples: int
    _last_reported_silence_ms: int
    _level_window: deque[tuple[int, float]]
    _level_samples: int

    def __init__(self, config: SilenceDetectorConfig, *, sample_rate: int = 16000) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._noise_ema = None
        self._run_samples = 0
        self._run_committed = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._last_reported_silence_ms = -1
        self._level_window = deque()
        self._level_samples = 0

    @property
    def silence_ms(self) -> int:
        return self._silence_samples * 1000 // self._sample_rate

    @property
    def _speech_ms(self) -> int:
        return self._speech_samples * 1000 // self._sample_rate

    @property
    def _silence_threshold(self) -> float:
        if self._noise_ema is None:
            return MIN_SILENCE_FLOOR
        return max(MIN_SILENCE_FLOOR, self._noise_ema * NOISE_FLOOR_MULTIPLIER)

    @property
    def _speech_threshold(self) -> float:
        return self._silence_threshold * SPEECH_HYSTERESIS_FACTOR

    def _make_result(self, rms: float, peak: float, is_speech: bool) -> SilenceDetectorResult:
        speech_ms = self._speech_ms
        silence_ms = self.silence_ms
        heard_speech = speech_ms >= MIN_SPEECH_MS
        return SilenceDetectorResult(
            rms=rms,
            peak=peak,
            is_speech=is_speech,
            silence_ms=silence_ms,
            speech_ms=speech_ms,
            should_stop=heard_speech and silence_ms >= self._config.silence_stop_ms,
            heard_speech=heard_speech,
        )

    def process_chunk(self, pcm: bytes) -> SilenceDetectorResult:
        # crosshair: off
        if not self._config.enabled or len(pcm) < 2:
            return self._make_result(0.0, 0.0, False)
        sample_count = len(pcm) // 2

        rms, peak = pcm_energy_int16(pcm)
        self._push_level(sample_count, rms)

        # The old code froze the 0.003 floor on the first chunk at or above 0.0054,
        # including ordinary room noise, so later noise was still speech and auto-stop
        # never fired. This classifies only energy at or above LOUD_ONSET_RMS, or
        # energy above the floor learned from quieter chunks, and it ignores runs
        # shorter than SPEECH_ONSET_MS.
        # Bugfix: a quieter chunk snapped _noise_ema down (a leading silent
        # callback snaps it to 0) and _adapt_floor returned while is_speech was
        # true, so the floor could not rise. Energy above the speech threshold
        # (0.0054 at MIN_SILENCE_FLOOR), including amplitude-328 room noise,
        # stayed speech and auto-stop never fired. A full STEADY_NOISE_MS window
        # of similar RMS below LOUD_ONSET_RMS is ambient: _is_speech returns
        # False and _adapt_floor snaps _noise_ema to that window's minimum.
        # Hysteresis still covers a shorter dip, because the window still holds
        # the louder audio.
        steady, ambient_low = self._window_ambient()
        is_speech = self._is_speech(rms, peak, steady)
        self._adapt_floor(rms, is_speech, sample_count, steady, ambient_low)

        if is_speech:
            self._run_samples += sample_count
            if self._run_samples * 1000 >= SPEECH_ONSET_MS * self._sample_rate:
                if not self._run_committed:
                    self._speech_samples += self._run_samples
                    self._run_committed = True
                    self._last_reported_silence_ms = -1
                    log.info(
                        "audio VAD: speech started (rms=%.4f peak=%.4f silence_thr=%.4f speech_thr=%.4f)",
                        rms,
                        peak,
                        self._silence_threshold,
                        self._speech_threshold,
                    )
                else:
                    self._speech_samples += sample_count
                self._silence_samples = 0
        else:
            if self._run_committed:
                log.info(
                    "audio VAD: speech ended (speech_ms=%d rms=%.4f peak=%.4f)",
                    self._speech_ms,
                    rms,
                    peak,
                )
            self._run_samples = 0
            self._run_committed = False
            self._silence_samples += sample_count

        result = self._make_result(rms, peak, is_speech)
        if result.should_stop:
            log.info(
                "audio VAD: auto-stop triggered (speech_ms=%d silence_ms=%d)",
                result.speech_ms,
                result.silence_ms,
            )

        return result

    def should_emit_silence_progress(self, result: SilenceDetectorResult) -> bool:
        """Throttle IPC/UI updates to meaningful silence milestones."""
        if not self._config.enabled or result.is_speech:
            return False
        if result.silence_ms < 100:
            return False
        step = max(100, self._config.silence_stop_ms // 4)
        if result.silence_ms - self._last_reported_silence_ms >= step:
            self._last_reported_silence_ms = result.silence_ms
            return True
        return False

    def _push_level(self, sample_count: int, rms: float) -> None:
        self._level_window.append((sample_count, rms))
        self._level_samples += sample_count
        limit = STEADY_NOISE_MS * self._sample_rate // 1000
        while len(self._level_window) > 1 and self._level_samples - self._level_window[0][0] >= limit:
            self._level_samples -= self._level_window.popleft()[0]

    def _window_ambient(self) -> tuple[bool, float]:
        limit = STEADY_NOISE_MS * self._sample_rate // 1000
        if self._level_samples < limit:
            return False, 0.0
        low = min(level for _count, level in self._level_window)
        high = max(level for _count, level in self._level_window)
        if high >= LOUD_ONSET_RMS:
            return False, low
        return high <= low * NOISE_FLOOR_MULTIPLIER, low

    def _adapt_floor(self, rms: float, is_speech: bool, sample_count: int, steady: bool, ambient_low: float) -> None:
        if steady:
            self._noise_ema = ambient_low
            return
        if is_speech:
            return
        if self._noise_ema is None or rms < self._noise_ema:
            self._noise_ema = rms
            return
        dt = sample_count / self._sample_rate
        alpha = 1.0 - math.exp(-dt / NOISE_EMA_TAU_SEC)
        self._noise_ema = (1.0 - alpha) * self._noise_ema + alpha * rms

    def _is_speech(self, rms: float, peak: float, steady: bool) -> bool:
        if rms >= LOUD_ONSET_RMS:
            return True
        if steady:
            return False
        if self._noise_ema is None:
            return False
        if rms >= self._speech_threshold:
            return True
        if rms <= self._silence_threshold and peak <= self._silence_threshold * PEAK_SILENCE_FACTOR:
            return False
        return self._run_committed
