"""Shared Kokoro / Piper cache discovery. No network."""

from __future__ import annotations

from pathlib import Path

from plugin.audio.model_cache_paths import KOKORO_MODEL_FILENAME, KOKORO_VOICES_FILENAME, _directory_is_writable, kokoro_should_download, resolve_kokoro_model_paths, resolve_piper_voice_paths, shared_cache_root


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows
    # (ntpath.expanduser). HOME alone still resolves the runner profile.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("KOKORO_MODEL_PATH", raising=False)
    monkeypatch.delenv("KOKORO_VOICES_PATH", raising=False)
    return home


def _touch_kokoro(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    model = directory / KOKORO_MODEL_FILENAME
    voices = directory / KOKORO_VOICES_FILENAME
    model.write_bytes(b"model")
    voices.write_bytes(b"voices")
    return model, voices


def test_kokoro_found_in_pipecat_dir(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    pipecat = home / ".cache" / "pipecat" / "kokoro-onnx"
    model, voices = _touch_kokoro(pipecat)

    found_model, found_voices = resolve_kokoro_model_paths()

    assert found_model == model
    assert found_voices == voices
    assert kokoro_should_download(found_model, "KOKORO_MODEL_PATH") is False
    assert kokoro_should_download(found_voices, "KOKORO_VOICES_PATH") is False
    assert not (home / ".cache" / "kokoro").exists()


def test_kokoro_canonical_dir_beats_later_probes(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    canonical_model, canonical_voices = _touch_kokoro(home / ".cache" / "kokoro")
    _touch_kokoro(home / ".cache" / "kokoro-tts")
    _touch_kokoro(home / ".cache" / "pipecat" / "kokoro-onnx")

    found_model, found_voices = resolve_kokoro_model_paths()

    assert found_model == canonical_model
    assert found_voices == canonical_voices


def test_kokoro_pairs_files_from_different_probe_dirs(monkeypatch, tmp_path):
    """First hit is per file. Both exist, so nothing is downloaded."""
    home = _home(monkeypatch, tmp_path)
    model_dir = home / ".cache" / "kokoro"
    model_dir.mkdir(parents=True)
    model = model_dir / KOKORO_MODEL_FILENAME
    model.write_bytes(b"model")
    voices_dir = home / ".cache" / "pipecat" / "kokoro-onnx"
    voices_dir.mkdir(parents=True)
    voices = voices_dir / KOKORO_VOICES_FILENAME
    voices.write_bytes(b"voices")

    found_model, found_voices = resolve_kokoro_model_paths()

    assert found_model == model
    assert found_voices == voices
    assert kokoro_should_download(found_model, "KOKORO_MODEL_PATH") is False
    assert kokoro_should_download(found_voices, "KOKORO_VOICES_PATH") is False


def test_kokoro_missing_sibling_completes_in_writable_probe_dir(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    pipecat = home / ".cache" / "pipecat" / "kokoro-onnx"
    pipecat.mkdir(parents=True)
    model = pipecat / KOKORO_MODEL_FILENAME
    model.write_bytes(b"model")

    found_model, found_voices = resolve_kokoro_model_paths()

    assert found_model == model
    assert found_voices == pipecat / KOKORO_VOICES_FILENAME
    assert kokoro_should_download(found_voices, "KOKORO_VOICES_PATH") is True
    assert not found_voices.exists()


def test_kokoro_missing_sibling_falls_back_to_canonical_when_not_writable(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    pipecat = home / ".cache" / "pipecat" / "kokoro-onnx"
    pipecat.mkdir(parents=True)
    model = pipecat / KOKORO_MODEL_FILENAME
    model.write_bytes(b"model")
    monkeypatch.setattr("plugin.audio.model_cache_paths._directory_is_writable", lambda _directory: False)

    found_model, found_voices = resolve_kokoro_model_paths()

    assert found_model == model
    assert found_voices == home / ".cache" / "kokoro" / KOKORO_VOICES_FILENAME
    assert kokoro_should_download(found_voices, "KOKORO_VOICES_PATH") is True


def test_kokoro_download_target_is_canonical_when_nothing_found(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)

    model, voices = resolve_kokoro_model_paths()

    assert model == home / ".cache" / "kokoro" / KOKORO_MODEL_FILENAME
    assert voices == home / ".cache" / "kokoro" / KOKORO_VOICES_FILENAME
    assert kokoro_should_download(model, "KOKORO_MODEL_PATH") is True
    assert kokoro_should_download(voices, "KOKORO_VOICES_PATH") is True


def test_kokoro_env_override_wins_over_probe(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _touch_kokoro(home / ".cache" / "pipecat" / "kokoro-onnx")
    custom_model = tmp_path / "custom-model.onnx"
    custom_voices = tmp_path / "custom-voices.bin"
    custom_model.write_bytes(b"m")
    custom_voices.write_bytes(b"v")
    monkeypatch.setenv("KOKORO_MODEL_PATH", str(custom_model))
    monkeypatch.setenv("KOKORO_VOICES_PATH", str(custom_voices))

    model, voices = resolve_kokoro_model_paths()

    assert model == custom_model
    assert voices == custom_voices
    assert kokoro_should_download(model, "KOKORO_MODEL_PATH") is False
    assert kokoro_should_download(voices, "KOKORO_VOICES_PATH") is False


def test_kokoro_missing_env_path_is_not_replaced_by_probe(monkeypatch, tmp_path):
    """A set-but-missing override keeps today's path and does not download it."""
    home = _home(monkeypatch, tmp_path)
    _touch_kokoro(home / ".cache" / "pipecat" / "kokoro-onnx")
    missing = tmp_path / "missing-model.onnx"
    monkeypatch.setenv("KOKORO_MODEL_PATH", str(missing))

    model, voices = resolve_kokoro_model_paths()

    assert model == missing
    assert voices == home / ".cache" / "pipecat" / "kokoro-onnx" / KOKORO_VOICES_FILENAME
    assert kokoro_should_download(model, "KOKORO_MODEL_PATH") is False
    assert kokoro_should_download(voices, "KOKORO_VOICES_PATH") is False


def test_kokoro_env_pointing_at_canonical_missing_file_is_downloaded(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    canonical = home / ".cache" / "kokoro" / KOKORO_MODEL_FILENAME
    monkeypatch.setenv("KOKORO_MODEL_PATH", str(canonical))

    model, voices = resolve_kokoro_model_paths()

    assert model == canonical
    assert voices == home / ".cache" / "kokoro" / KOKORO_VOICES_FILENAME
    assert kokoro_should_download(model, "KOKORO_MODEL_PATH") is True
    assert kokoro_should_download(voices, "KOKORO_VOICES_PATH") is True


def test_piper_found_in_pipecat_dir(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    voice_dir = home / ".cache" / "pipecat" / "piper"
    voice_dir.mkdir(parents=True)
    onnx = voice_dir / "en_US-lessac-medium.onnx"
    config = voice_dir / "en_US-lessac-medium.onnx.json"
    onnx.write_bytes(b"onnx")
    config.write_bytes(b"{}")

    found_onnx, found_config = resolve_piper_voice_paths("en_US-lessac-medium")

    assert found_onnx == onnx
    assert found_config == config
    assert not (home / ".cache" / "piper").exists()


def test_piper_canonical_dir_beats_pipecat(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    canonical = home / ".cache" / "piper"
    canonical.mkdir(parents=True)
    ours = canonical / "en_US-lessac-medium.onnx"
    ours_json = canonical / "en_US-lessac-medium.onnx.json"
    ours.write_bytes(b"ours")
    ours_json.write_bytes(b"{}")
    pipecat = home / ".cache" / "pipecat" / "piper"
    pipecat.mkdir(parents=True)
    (pipecat / "en_US-lessac-medium.onnx").write_bytes(b"theirs")
    (pipecat / "en_US-lessac-medium.onnx.json").write_bytes(b"{}")

    found_onnx, found_config = resolve_piper_voice_paths("en_US-lessac-medium")

    assert found_onnx == ours
    assert found_config == ours_json


def test_piper_partial_pair_is_not_a_hit(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    voice_dir = home / ".cache" / "pipecat" / "piper"
    voice_dir.mkdir(parents=True)
    (voice_dir / "en_US-lessac-medium.onnx").write_bytes(b"onnx")

    onnx, config = resolve_piper_voice_paths("en_US-lessac-medium")

    assert onnx == home / ".cache" / "piper" / "en_US-lessac-medium.onnx"
    assert config == home / ".cache" / "piper" / "en_US-lessac-medium.onnx.json"


def test_piper_download_target_is_canonical_when_nothing_found(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)

    onnx, config = resolve_piper_voice_paths("de_DE-thorsten-medium")

    assert onnx == home / ".cache" / "piper" / "de_DE-thorsten-medium.onnx"
    assert config == home / ".cache" / "piper" / "de_DE-thorsten-medium.onnx.json"


def test_xdg_cache_home_redirects_probe_and_download(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    # A hit under the default home cache must be ignored once XDG is set.
    ignored = home / ".cache" / "pipecat" / "kokoro-onnx"
    _touch_kokoro(ignored)
    ignored_piper = home / ".cache" / "pipecat" / "piper"
    ignored_piper.mkdir(parents=True)
    (ignored_piper / "en_US-lessac-medium.onnx").write_bytes(b"onnx")
    (ignored_piper / "en_US-lessac-medium.onnx.json").write_bytes(b"{}")

    xdg = tmp_path / "xdg-cache"
    hit = xdg / "pipecat" / "kokoro-onnx"
    model, voices = _touch_kokoro(hit)
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))

    assert shared_cache_root() == xdg
    found_model, found_voices = resolve_kokoro_model_paths()
    assert found_model == model
    assert found_voices == voices

    # Nothing under the XDG root: download targets move with it.
    for path in hit.iterdir():
        path.unlink()
    hit.rmdir()
    download_model, download_voices = resolve_kokoro_model_paths()
    assert download_model == xdg / "kokoro" / KOKORO_MODEL_FILENAME
    assert download_voices == xdg / "kokoro" / KOKORO_VOICES_FILENAME
    onnx, config = resolve_piper_voice_paths("en_US-lessac-medium")
    assert onnx == xdg / "piper" / "en_US-lessac-medium.onnx"
    assert config == xdg / "piper" / "en_US-lessac-medium.onnx.json"
    # The home-cache Piper pair is not used while XDG points elsewhere.
    assert onnx != ignored_piper / "en_US-lessac-medium.onnx"


def test_blank_xdg_cache_home_uses_home_cache(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", "   ")

    assert shared_cache_root() == home / ".cache"


def test_directory_is_writable_matches_create(tmp_path):
    assert _directory_is_writable(tmp_path) is True
    assert _directory_is_writable(tmp_path / "missing") is False
    assert list(tmp_path.glob(".writeragent-write-probe-*")) == []
