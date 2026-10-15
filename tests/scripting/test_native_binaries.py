import email
import io
import os
import sys
import urllib.error
from unittest.mock import patch

import pytest

from plugin.scripting.native_binaries import (
    _atomic_replace_native,
    _cleanup_stale_native_backups,
    _download_url_to_file,
    ensure_native_binaries_on_path,
)


def test_ensure_native_binaries_on_path_adds_bin_dir(tmp_path):
    bin_dir = tmp_path / "audio_binaries"
    bin_dir.mkdir()
    ucd = str(tmp_path)
    original_path = list(sys.path)
    try:
        sys.path[:] = [p for p in original_path if p != str(bin_dir)]
        with patch("plugin.framework.config.user_config_dir", return_value=ucd):
            ensure_native_binaries_on_path()
        assert str(bin_dir) in sys.path
    finally:
        sys.path[:] = original_path


def test_ensure_native_binaries_on_path_idempotent(tmp_path):
    bin_dir = tmp_path / "audio_binaries"
    bin_dir.mkdir()
    ucd = str(tmp_path)
    with patch("plugin.framework.config.user_config_dir", return_value=ucd):
        ensure_native_binaries_on_path()
        first_index = sys.path.index(str(bin_dir))
        ensure_native_binaries_on_path()
        assert sys.path.index(str(bin_dir)) == first_index
        assert sys.path.count(str(bin_dir)) == 1


def test_ensure_native_binaries_on_path_adds_in_tree_contrib():
    original_path = list(sys.path)
    try:
        ensure_native_binaries_on_path()
        # Verify in-tree contrib or contrib/vec_pack exists on sys.path
        mod_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        contrib_dir = os.path.join(mod_dir, "contrib")
        if os.path.isdir(contrib_dir):
            assert contrib_dir in sys.path
    finally:
        sys.path[:] = original_path


def test_download_url_to_file_atomic_replace_preserves_inode(tmp_path):
    """Redownload must not truncate an existing file in place (mapped .so SIGBUS)."""
    dest = tmp_path / "pack.so"
    dest.write_bytes(b"OLD_NATIVE_BYTES")
    # POSIX: hold an fd like mmap so truncate-in-place would ruin this view.
    # Windows: a generic RDONLY handle lacks FILE_SHARE_DELETE, so even the
    # loaded-DLL rename-aside path hits WinError 32 (CI 33453184665). That
    # Windows path is covered by test_atomic_replace_native_windows_*.
    hold_fd = os.name != "nt"
    fd = os.open(str(dest), os.O_RDONLY) if hold_fd else None
    try:

        class _Resp:
            headers = {"content-length": "4"}

            def read(self, n=-1):
                if not hasattr(self, "_done"):
                    self._done = True
                    return b"NEW!"
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        statuses: list[str] = []
        with patch("urllib.request.urlopen", return_value=_Resp()) as mock_urlopen:
            _download_url_to_file("https://example.test/pack.so", str(dest), statuses.append)
        assert mock_urlopen.call_args.kwargs["timeout"] == 60

        assert dest.read_bytes() == b"NEW!"
        if fd is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            assert os.read(fd, 100) == b"OLD_NATIVE_BYTES"
        assert list(tmp_path.glob("*.partial")) == []
    finally:
        if fd is not None:
            os.close(fd)


def test_download_url_to_file_cleans_partial_on_failure(tmp_path):
    dest = tmp_path / "pack.so"
    dest.write_bytes(b"KEEP")

    class _Boom:
        headers = {"content-length": "0"}

        def read(self, n=-1):
            raise OSError("network down")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("urllib.request.urlopen", return_value=_Boom()):
        with pytest.raises(RuntimeError, match="Failed to download"):
            _download_url_to_file("https://example.test/pack.so", str(dest), lambda _s: None)

    assert dest.read_bytes() == b"KEEP"
    assert list(tmp_path.glob("*.partial")) == []


def test_download_rejects_bad_sha_and_keeps_dest(tmp_path):
    dest = tmp_path / "pack.so"
    dest.write_bytes(b"KEEP")

    class _BadBody:
        headers = {"content-length": "4"}

        def read(self, n=-1):
            if not hasattr(self, "_done"):
                self._done = True
                return b"NOPE"
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("urllib.request.urlopen", return_value=_BadBody()):
        with pytest.raises(RuntimeError, match="sha256"):
            _download_url_to_file(
                "https://example.test/pack.so",
                str(dest),
                lambda _s: None,
                expected_sha256="0" * 64,
                max_bytes=1024,
            )

    assert dest.read_bytes() == b"KEEP"
    assert list(tmp_path.glob("*.partial")) == []


def test_download_stops_when_body_exceeds_cap(tmp_path):
    dest = tmp_path / "pack.so"
    dest.write_bytes(b"KEEP")

    class _Endless:
        headers = {}

        def read(self, n=-1):
            return b"x" * 100

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("urllib.request.urlopen", return_value=_Endless()):
        with pytest.raises(RuntimeError, match="exceeded"):
            _download_url_to_file(
                "https://example.test/pack.so",
                str(dest),
                lambda _s: None,
                max_bytes=150,
                expected_sha256="0" * 64,
            )

    assert dest.read_bytes() == b"KEEP"
    assert list(tmp_path.glob("*.partial")) == []


@pytest.mark.parametrize(
    ("side_effect", "match"),
    [
        (urllib.error.URLError("timed out"), "Failed to download"),
        (
            urllib.error.HTTPError(
                "https://example.test/pack.so",
                404,
                "Not Found",
                email.message_from_string(""),
                io.BytesIO(),
            ),
            "HTTP Error 404",
        ),
    ],
)
def test_download_urlopen_failure_closes_fd_and_removes_partial(tmp_path, side_effect, match):
    dest = tmp_path / "pack.so"
    dest.write_bytes(b"KEEP")

    def _get_fd_count() -> int | None:
        if not os.path.isdir("/proc/self/fd"):
            return None
        return len(os.listdir("/proc/self/fd"))

    count_before = _get_fd_count()
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(RuntimeError, match=match):
            _download_url_to_file("https://example.test/pack.so", str(dest), lambda _s: None)
    count_after = _get_fd_count()

    assert dest.read_bytes() == b"KEEP"
    assert list(tmp_path.glob("*.partial")) == []
    if count_before is not None:
        assert count_after == count_before



def test_run_vec_pack_download_invalidates_accelerator(tmp_path):
    from plugin.scripting import native_binaries
    from plugin.scripting.native_binaries import run_vec_pack_download

    assert "/master/" not in native_binaries._CONTRIB_BASE_URL
    assert native_binaries._CONTRIB_COMMIT in native_binaries._CONTRIB_BASE_URL

    with (
        patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)),
        patch("sysconfig.get_config_var", return_value=".cpython-312-x86_64-linux-gnu.so"),
        patch("plugin.scripting.native_binaries._download_url_to_file") as mock_dl,
        patch("plugin.scripting.native_binaries.ensure_native_binaries_on_path"),
        patch("plugin.scripting.payload_codec.invalidate_host_cython_accelerator") as mock_inv,
    ):
        ok = run_vec_pack_download(lambda _t: None, lambda _s: None)
    assert ok is True
    mock_inv.assert_called_once()
    calls = mock_dl.call_args_list
    assert calls[0].kwargs["expected_sha256"] == native_binaries._VEC_PACK_SHA256["__init__.py"]
    assert (
        calls[1].kwargs["expected_sha256"]
        == native_binaries._VEC_PACK_SHA256["pack.cpython-312-x86_64-linux-gnu.so"]
    )


def test_run_vec_pack_download_bind_host_false_skips_path_and_modules(tmp_path):
    from plugin.scripting.native_binaries import run_vec_pack_download

    with (
        patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)),
        patch("sysconfig.get_config_var", return_value=".cpython-312-x86_64-linux-gnu.so"),
        patch("plugin.scripting.native_binaries._download_url_to_file"),
        patch("plugin.scripting.native_binaries.ensure_native_binaries_on_path") as mock_ensure,
        patch("plugin.scripting.payload_codec.invalidate_host_cython_accelerator") as mock_inv,
    ):
        ok = run_vec_pack_download(lambda _t: None, lambda _s: None, bind_host=False)
    assert ok is True
    mock_ensure.assert_not_called()
    mock_inv.assert_not_called()


def test_atomic_replace_native_posix_uses_os_replace(tmp_path):
    partial = tmp_path / "pack.so.partial"
    dest = tmp_path / "pack.so"
    partial.write_bytes(b"NEW")
    dest.write_bytes(b"OLD")

    with patch("os.name", "posix"):
        _atomic_replace_native(str(partial), str(dest))

    assert dest.read_bytes() == b"NEW"
    assert not partial.exists()


def test_atomic_replace_native_windows_renames_loaded_dll_aside(tmp_path):
    """On Windows a loaded .pyd can't be replaced; rename it aside then drop the new file."""
    partial = tmp_path / "pack.pyd.partial"
    dest = tmp_path / "pack.pyd"
    partial.write_bytes(b"NEW")
    dest.write_bytes(b"LOADED_OLD")

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        if calls["n"] == 0 and src == str(partial) and dst == str(dest):
            calls["n"] += 1
            raise PermissionError("WinError 5: access denied (loaded DLL)")
        return real_replace(src, dst)

    with patch("os.name", "nt"), patch("os.replace", side_effect=flaky_replace):
        _atomic_replace_native(str(partial), str(dest))

    assert dest.read_bytes() == b"NEW"
    assert not partial.exists()
    aside = tmp_path / "pack.pyd.old"
    assert aside.read_bytes() == b"LOADED_OLD"


def test_atomic_replace_native_windows_picks_unique_aside(tmp_path):
    partial = tmp_path / "pack.pyd.partial"
    dest = tmp_path / "pack.pyd"
    partial.write_bytes(b"NEW")
    dest.write_bytes(b"LOADED_OLD")
    (tmp_path / "pack.pyd.old").write_bytes(b"PRIOR_STILL_LOADED")

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        if calls["n"] == 0 and src == str(partial) and dst == str(dest):
            calls["n"] += 1
            raise PermissionError("WinError 5")
        return real_replace(src, dst)

    with patch("os.name", "nt"), patch("os.replace", side_effect=flaky_replace):
        _atomic_replace_native(str(partial), str(dest))

    assert dest.read_bytes() == b"NEW"
    assert (tmp_path / "pack.pyd.old").read_bytes() == b"PRIOR_STILL_LOADED"
    assert (tmp_path / "pack.pyd.old.1").read_bytes() == b"LOADED_OLD"


def test_atomic_replace_native_windows_restore_puts_old_bytes_back(tmp_path):
    partial = tmp_path / "pack.pyd.partial"
    dest = tmp_path / "pack.pyd"
    partial.write_bytes(b"NEW")
    dest.write_bytes(b"LOADED_OLD")

    real_replace = os.replace

    def flaky_replace(src, dst):
        if src == str(partial):
            raise PermissionError("loaded DLL")
        return real_replace(src, dst)

    with (
        patch("os.name", "nt"),
        patch("os.replace", side_effect=flaky_replace),
        patch("plugin.scripting.native_binaries.log.exception") as mock_log,
    ):
        with pytest.raises(PermissionError, match="loaded DLL"):
            _atomic_replace_native(str(partial), str(dest))

    assert dest.read_bytes() == b"LOADED_OLD"
    assert not (tmp_path / "pack.pyd.old").exists()
    assert partial.exists()
    mock_log.assert_not_called()


def test_atomic_replace_native_windows_restore_failure_keeps_aside(tmp_path):
    partial = tmp_path / "pack.pyd.partial"
    dest = tmp_path / "pack.pyd"
    partial.write_bytes(b"NEW")
    dest.write_bytes(b"LOADED_OLD")
    aside = tmp_path / "pack.pyd.old"

    real_replace = os.replace

    def flaky_replace(src, dst):
        if src == str(partial):
            raise PermissionError("loaded DLL")
        if src == str(aside):
            raise PermissionError("restore failed")
        return real_replace(src, dst)

    with (
        patch("os.name", "nt"),
        patch("os.replace", side_effect=flaky_replace),
        patch("plugin.scripting.native_binaries.log.exception") as mock_log,
    ):
        with pytest.raises(PermissionError, match="loaded DLL"):
            _atomic_replace_native(str(partial), str(dest))

    assert not dest.exists()
    assert aside.read_bytes() == b"LOADED_OLD"
    assert partial.exists()
    mock_log.assert_called_once()


def test_cleanup_stale_native_backups_removes_old_files(tmp_path, monkeypatch):
    import plugin.scripting.native_binaries as nb
    monkeypatch.setattr(nb, "_swept", False)

    vec_dir = tmp_path / "writeragent_vec"
    vec_dir.mkdir()
    (vec_dir / "pack.cp314-win_amd64.pyd").write_bytes(b"live")
    (vec_dir / "pack.cp314-win_amd64.pyd.old").write_bytes(b"stale")
    (vec_dir / "pack.cp314-win_amd64.pyd.old.1").write_bytes(b"stale1")
    (tmp_path / "_cffi_backend.pyd").write_bytes(b"live_cffi")
    (tmp_path / "_cffi_backend.pyd.old").write_bytes(b"stale2")
    (tmp_path / "helper.older.py").write_bytes(b"keep_me")
    (tmp_path / "archive.oldest").write_bytes(b"keep_me_too")

    with patch("os.name", "nt"):
        _cleanup_stale_native_backups(str(tmp_path))

    assert (vec_dir / "pack.cp314-win_amd64.pyd").exists()
    assert not (vec_dir / "pack.cp314-win_amd64.pyd.old").exists()
    assert not (vec_dir / "pack.cp314-win_amd64.pyd.old.1").exists()
    assert (tmp_path / "_cffi_backend.pyd").exists()
    assert not (tmp_path / "_cffi_backend.pyd.old").exists()
    assert (tmp_path / "helper.older.py").exists()
    assert (tmp_path / "archive.oldest").exists()


def test_cleanup_stale_native_backups_tolerates_locked_file(tmp_path, monkeypatch):
    import plugin.scripting.native_binaries as nb
    monkeypatch.setattr(nb, "_swept", False)

    (tmp_path / "pack.pyd").write_bytes(b"live")
    (tmp_path / "pack.pyd.old").write_bytes(b"stale")

    def boom(_path):
        raise PermissionError("still mapped")

    with patch("os.name", "nt"), patch("os.remove", side_effect=boom):
        _cleanup_stale_native_backups(str(tmp_path))

    assert (tmp_path / "pack.pyd.old").exists()


def test_cleanup_restores_newest_aside_when_dest_missing(tmp_path, monkeypatch):
    import plugin.scripting.native_binaries as nb
    monkeypatch.setattr(nb, "_swept", False)

    (tmp_path / "pack.pyd.old").write_bytes(b"OLDER")
    (tmp_path / "pack.pyd.old.1").write_bytes(b"NEWER")
    (tmp_path / "_cffi_backend.pyd.old").write_bytes(b"CFFI")
    (tmp_path / "other.pyd.old.2").write_bytes(b"GEN2")
    (tmp_path / "helper.older.py").write_bytes(b"keep_me")
    (tmp_path / ".old").write_bytes(b"dotold")

    with patch("os.name", "nt"):
        _cleanup_stale_native_backups(str(tmp_path))

    assert (tmp_path / "pack.pyd").read_bytes() == b"NEWER"
    assert not (tmp_path / "pack.pyd.old").exists()
    assert not (tmp_path / "pack.pyd.old.1").exists()

    assert (tmp_path / "_cffi_backend.pyd").read_bytes() == b"CFFI"
    assert not (tmp_path / "_cffi_backend.pyd.old").exists()

    assert (tmp_path / "other.pyd").read_bytes() == b"GEN2"
    assert not (tmp_path / "other.pyd.old.2").exists()

    assert (tmp_path / "helper.older.py").read_bytes() == b"keep_me"
    assert (tmp_path / ".old").read_bytes() == b"dotold"


def test_cleanup_leaves_asides_when_restore_fails(tmp_path, monkeypatch):
    import plugin.scripting.native_binaries as nb
    monkeypatch.setattr(nb, "_swept", False)

    (tmp_path / "pack.pyd.old").write_bytes(b"ONLY")

    def boom_replace(_src, _dst):
        raise PermissionError("cannot restore")

    def boom_remove(_path):
        raise AssertionError("should not attempt to remove older asides")

    with (
        patch("os.name", "nt"),
        patch("os.replace", side_effect=boom_replace),
        patch("os.remove", side_effect=boom_remove),
    ):
        _cleanup_stale_native_backups(str(tmp_path))

    assert (tmp_path / "pack.pyd.old").read_bytes() == b"ONLY"
    assert not (tmp_path / "pack.pyd").exists()


def test_cleanup_stale_native_backups_noop_on_posix(tmp_path, monkeypatch):
    import plugin.scripting.native_binaries as nb
    monkeypatch.setattr(nb, "_swept", False)

    (tmp_path / "pack.so.old").write_bytes(b"stale")

    with patch("os.name", "posix"):
        _cleanup_stale_native_backups(str(tmp_path))

    assert (tmp_path / "pack.so.old").exists()


def test_wave_open_failure_message_does_not_mention_portaudio():
    import threading
    from unittest.mock import MagicMock, patch
    from plugin.scripting.venv.audio_recorder import record_to_wav

    fake_sd = MagicMock()
    with patch("plugin.scripting.venv.audio_recorder._import_sounddevice", return_value=fake_sd):
        with pytest.raises(RuntimeError) as exc_info:
            record_to_wav("/nonexistent_dir_cannot_create/test.wav", threading.Event())
        msg = str(exc_info.value)
        assert "Failed to create audio WAV file" in msg
        assert "PortAudio" not in msg


@pytest.mark.parametrize(
    ("system", "machine", "ext_suffix", "portaudio_basename"),
    [
        ("Linux", "x86_64", ".cpython-312-x86_64-linux-gnu.so", None),
        ("Darwin", "arm64", ".cpython-312-darwin.so", "libportaudio.dylib"),
        ("Windows", "AMD64", ".cp312-win_amd64.pyd", "libportaudio64bit.dll"),
        ("Windows", "ARM64", ".cp312-win_arm64.pyd", "libportaudioarm64.dll"),
    ],
)
def test_run_audio_download_selects_cffi_and_portaudio(
    tmp_path, system, machine, ext_suffix, portaudio_basename
):
    from plugin.scripting import native_binaries
    from plugin.scripting.native_binaries import run_audio_download

    native_dir = os.path.join(str(tmp_path), "audio_binaries")
    zip_path = os.path.join(native_dir, "audio_source.zip")
    cffi_name = f"_cffi_backend{ext_suffix}"
    cffi_path = os.path.join(native_dir, cffi_name)

    extracted_targets: list[str] = []
    zip_opened: list[tuple[str, str]] = []

    class _MockZip:
        def __init__(self, path, mode):
            zip_opened.append((path, mode))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extractall(self, target):
            extracted_targets.append(target)

    dl_calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_download(url, dest_path, on_status, **kwargs):
        dl_calls.append((url, dest_path, kwargs))

    with (
        patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)),
        patch("sysconfig.get_config_var", return_value=ext_suffix),
        patch("platform.system", return_value=system),
        patch("platform.machine", return_value=machine),
        patch("zipfile.ZipFile", _MockZip),
        patch("plugin.scripting.native_binaries._download_url_to_file", side_effect=fake_download),
        patch("plugin.scripting.native_binaries.run_vec_pack_download", return_value=True) as mock_vec,
    ):
        result = run_audio_download(lambda _t: None, lambda _s: None)

    assert result is True

    expected_dests = [zip_path, cffi_path]
    expected_basenames = ["audio_source.zip", cffi_name]
    if portaudio_basename is not None:
        pa_path = os.path.join(native_dir, "_sounddevice_data", "portaudio-binaries", portaudio_basename)
        expected_dests.append(pa_path)
        expected_basenames.append(portaudio_basename)

    actual_dests = [call[1] for call in dl_calls]
    assert actual_dests == expected_dests

    for i, basename in enumerate(expected_basenames):
        kwargs = dl_calls[i][2]
        assert kwargs["max_bytes"] == native_binaries._MAX_AUDIO_BYTES
        assert kwargs["expected_sha256"] == native_binaries._AUDIO_SHA256[basename]

    assert zip_opened == [(zip_path, "r")]
    assert extracted_targets == [native_dir]

    mock_vec.assert_called_once()
    assert mock_vec.call_args.kwargs == {"include_header": False, "bind_host": True}

    placeholder_file = tmp_path / "audio_binaries" / "_sounddevice_data" / "__init__.py"
    assert placeholder_file.read_text() == "# Placeholder\n"

    if system == "Linux":
        assert not any("portaudio" in dest for dest in actual_dests)


def test_run_audio_download_forwards_bind_host_false(tmp_path):
    from plugin.scripting.native_binaries import run_audio_download

    class _MockZip:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extractall(self, *args, **kwargs):
            pass

    with (
        patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)),
        patch("sysconfig.get_config_var", return_value=".cpython-312-x86_64-linux-gnu.so"),
        patch("platform.system", return_value="Linux"),
        patch("platform.machine", return_value="x86_64"),
        patch("zipfile.ZipFile", _MockZip),
        patch("plugin.scripting.native_binaries._download_url_to_file"),
        patch("plugin.scripting.native_binaries.run_vec_pack_download", return_value=True) as mock_vec,
    ):
        result = run_audio_download(lambda _t: None, lambda _s: None, bind_host=False)

    assert result is True
    mock_vec.assert_called_once()
    assert mock_vec.call_args.kwargs == {"include_header": False, "bind_host": False}


def test_run_audio_download_stops_before_vec_pack_when_cffi_download_fails(tmp_path):
    from plugin.scripting.native_binaries import run_audio_download

    class _MockZip:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extractall(self, *args, **kwargs):
            pass

    dl_count = 0

    def download_side_effect(*args, **kwargs):
        nonlocal dl_count
        dl_count += 1
        if dl_count == 2:
            raise RuntimeError("cffi failed")

    with (
        patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)),
        patch("sysconfig.get_config_var", return_value=".cpython-312-x86_64-linux-gnu.so"),
        patch("platform.system", return_value="Linux"),
        patch("platform.machine", return_value="x86_64"),
        patch("zipfile.ZipFile", _MockZip),
        patch("plugin.scripting.native_binaries._download_url_to_file", side_effect=download_side_effect),
        patch("plugin.scripting.native_binaries.run_vec_pack_download") as mock_vec,
    ):
        with pytest.raises(RuntimeError, match="cffi failed"):
            run_audio_download(lambda _t: None, lambda _s: None)

    mock_vec.assert_not_called()
    placeholder_file = tmp_path / "audio_binaries" / "_sounddevice_data" / "__init__.py"
    assert not placeholder_file.exists()


def test_unpinned_ext_suffix_raises_before_download(tmp_path):
    from plugin.scripting.native_binaries import run_audio_download, run_vec_pack_download

    with (
        patch("sysconfig.get_config_var", return_value=".pyd"),
        patch("plugin.framework.config.user_config_dir", return_value=str(tmp_path)),
        patch("plugin.scripting.native_binaries._download_url_to_file") as mock_dl,
    ):
        with pytest.raises(RuntimeError, match="No pinned sha256 for pack.pyd"):
            run_vec_pack_download(lambda _t: None, lambda _s: None)
        mock_dl.assert_not_called()

        with pytest.raises(RuntimeError, match="No pinned sha256 for _cffi_backend.pyd"):
            run_audio_download(lambda _t: None, lambda _s: None)
        mock_dl.assert_not_called()
