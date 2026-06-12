# Tests for macOS arm64-only audio contrib binaries.

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.update_audio_contrib import assert_macos_arm64

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRIB_AUDIO_DIR = REPO_ROOT / "plugin" / "contrib" / "audio"
PORTAUDIO_DYLIB = (
    CONTRIB_AUDIO_DIR / "_sounddevice_data" / "portaudio-binaries" / "libportaudio.dylib"
)


@pytest.fixture
def audio_contrib_dir():
    if not CONTRIB_AUDIO_DIR.is_dir():
        pytest.skip("plugin/contrib/audio not present — NO_RECORDING build")
    return CONTRIB_AUDIO_DIR


def test_mac_cffi_backends_are_arm64_only(audio_contrib_dir):
    darwin_backends = sorted(audio_contrib_dir.glob("_cffi_backend.cpython-*-darwin.so"))
    assert darwin_backends, "expected vendored cffi darwin backends"
    for path in darwin_backends:
        assert_macos_arm64(path)


def test_mac_portaudio_dylib_is_arm64_only(audio_contrib_dir):
    assert PORTAUDIO_DYLIB.is_file(), "expected vendored libportaudio.dylib"
    assert_macos_arm64(PORTAUDIO_DYLIB)
