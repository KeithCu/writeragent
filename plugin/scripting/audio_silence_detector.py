# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""RMS-based end-of-speech detection for sidebar microphone capture (venv and host paths)."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Algorithm tuning (not user config — adjust here if mic edge cases appear).
MIN_SPEECH_MS = 500
MIN_SILENCE_FLOOR = 0.003
SPEECH_HYSTERESIS_FACTOR = 1.8
PEAK_SPEECH_MULTIPLIER = 2.5
NOISE_FLOOR_MULTIPLIER = 1.5
SILENCE_EMA_ALPHA = 0.1
BOOTSTRAP_SPEECH_RMS = 0.008
BOOTSTRAP_SPEECH_PEAK = 0.03
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
    rms: float
    peak: float
    is_speech: bool
    silence_ms: int
    speech_ms: int
    should_stop: bool
    heard_speech: bool


def pcm_energy_int16(pcm: bytes) -> tuple[float, float]:
    """Return (RMS, peak) of 16-bit little-endian PCM, each normalized to 0.0–1.0."""
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return 0.0, 0.0
    samples = struct.unpack(f"<{sample_count}h", pcm)
    sum_sq = 0
    peak = 0
    for sample in samples:
        sum_sq += sample * sample
        abs_sample = sample if sample >= 0 else -sample
        if abs_sample > peak:
            peak = abs_sample
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

    def __init__(self, config: SilenceDetectorConfig, *, sample_rate: int = 16000) -> None:
        self._config = config
        self._sample_rate = sample_rate
        self._silence_ms = 0
        self._speech_ms = 0
        self._silence_threshold = MIN_SILENCE_FLOOR
        self._speech_threshold = MIN_SILENCE_FLOOR * SPEECH_HYSTERESIS_FACTOR
        self._silence_ema: float | None = None
        self._thresholds_frozen = False
        self._last_reported_silence_ms = -1
        self._in_speech = False
        self._session_max_rms = 0.0
        self._session_max_peak = 0.0

    @property
    def silence_ms(self) -> int:
        return self._silence_ms

    def process_chunk(self, pcm: bytes, *, frame_count: int) -> SilenceDetectorResult:
        if not self._config.enabled:
            return SilenceDetectorResult(
                rms=0.0, peak=0.0, is_speech=False, silence_ms=0, speech_ms=0, should_stop=False, heard_speech=False
            )

        duration_ms = int(frame_count * 1000 / self._sample_rate) if frame_count > 0 else 0
        rms, peak = pcm_energy_int16(pcm)
        self._session_max_rms = max(self._session_max_rms, rms)
        self._session_max_peak = max(self._session_max_peak, peak)

        is_speech = self._is_speech(rms, peak)
        if not is_speech and self._speech_ms == 0 and not self._thresholds_frozen:
            self._adapt_pre_speech_floor(rms)

        if is_speech:
            if not self._in_speech:
                self._in_speech = True
                self._thresholds_frozen = True
                log.info(
                    "audio VAD: speech started (rms=%.4f peak=%.4f silence_thr=%.4f speech_thr=%.4f)",
                    rms,
                    peak,
                    self._silence_threshold,
                    self._speech_threshold,
                )
            self._speech_ms += duration_ms
            self._silence_ms = 0
        else:
            if self._in_speech:
                self._in_speech = False
                log.info(
                    "audio VAD: speech ended (speech_ms=%d rms=%.4f peak=%.4f)",
                    self._speech_ms,
                    rms,
                    peak,
                )
            self._silence_ms += duration_ms

        heard_speech = self._speech_ms >= MIN_SPEECH_MS
        should_stop = heard_speech and self._silence_ms >= self._config.silence_stop_ms
        if should_stop:
            log.info(
                "audio VAD: auto-stop triggered (speech_ms=%d silence_ms=%d max_rms=%.4f max_peak=%.4f)",
                self._speech_ms,
                self._silence_ms,
                self._session_max_rms,
                self._session_max_peak,
            )

        return SilenceDetectorResult(
            rms=rms,
            peak=peak,
            is_speech=is_speech,
            silence_ms=self._silence_ms,
            speech_ms=self._speech_ms,
            should_stop=should_stop,
            heard_speech=heard_speech,
        )

    def should_emit_silence_progress(self, result: SilenceDetectorResult) -> bool:
        """Throttle IPC/UI updates to meaningful silence milestones."""
        if not self._config.enabled or result.is_speech:
            return False
        if result.silence_ms < 100:
            return False
        step = max(100, self._config.silence_stop_ms // 4)
        if result.silence_ms - self._last_reported_silence_ms >= step:
            self._last_reported_silence_ms = result.silence_ms
            log.debug(
                "audio VAD: silence progress (silence_ms=%d speech_ms=%d heard_speech=%s "
                "rms=%.4f peak=%.4f max_rms=%.4f max_peak=%.4f silence_thr=%.4f speech_thr=%.4f)",
                result.silence_ms,
                result.speech_ms,
                result.heard_speech,
                result.rms,
                result.peak,
                self._session_max_rms,
                self._session_max_peak,
                self._silence_threshold,
                self._speech_threshold,
            )
            return True
        return False

    def _adapt_pre_speech_floor(self, rms: float) -> None:
        if self._silence_ema is None:
            self._silence_ema = rms
        else:
            alpha = SILENCE_EMA_ALPHA
            self._silence_ema = (1.0 - alpha) * self._silence_ema + alpha * rms
        self._silence_threshold = max(MIN_SILENCE_FLOOR, self._silence_ema * NOISE_FLOOR_MULTIPLIER)
        self._speech_threshold = self._silence_threshold * SPEECH_HYSTERESIS_FACTOR

    def _is_speech(self, rms: float, peak: float) -> bool:
        silence_thr = self._silence_threshold
        speech_thr = self._speech_threshold
        peak_thr = max(silence_thr * PEAK_SPEECH_MULTIPLIER, 0.02)

        if rms >= speech_thr or peak >= peak_thr:
            return True
        if rms <= silence_thr and peak <= silence_thr * 1.5:
            return False
        if self._speech_ms == 0 and self._silence_ms == 0:
            return rms >= BOOTSTRAP_SPEECH_RMS or peak >= BOOTSTRAP_SPEECH_PEAK
        # Ambiguous band: stay in speech only while this segment has not yet counted silence.
        return self._speech_ms > 0 and self._silence_ms == 0
