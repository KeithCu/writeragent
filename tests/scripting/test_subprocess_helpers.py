# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for subprocess spawn helpers (env scrub, Flatpak/Snap escape)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from plugin.scripting.subprocess_helpers import (
    _PIPE_BUF_TARGET,
    _reset_cache,
    detect_sandbox,
    optimize_pipe,
    optimize_popen_pipes,
    wrap_command_for_sandbox,
)


class DetectSandboxTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=True)
    @patch.dict("os.environ", {}, clear=True)
    def test_detect_flatpak_via_file(self, mock_exists):
        self.assertEqual(detect_sandbox(), "flatpak")
        mock_exists.assert_called_with("/.flatpak-info")

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=False)
    @patch.dict("os.environ", {"FLATPAK_ID": "org.libreoffice.LibreOffice"}, clear=True)
    def test_detect_flatpak_via_env(self, _mock_exists):
        self.assertEqual(detect_sandbox(), "flatpak")

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=False)
    @patch.dict("os.environ", {"SNAP_NAME": "libreoffice"}, clear=True)
    def test_detect_snap(self, _mock_exists):
        self.assertEqual(detect_sandbox(), "snap")

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=False)
    @patch.dict("os.environ", {}, clear=True)
    def test_detect_none(self, _mock_exists):
        self.assertIsNone(detect_sandbox())

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=True)
    @patch.dict("os.environ", {}, clear=True)
    def test_result_is_cached(self, mock_exists):
        self.assertEqual(detect_sandbox(), "flatpak")
        self.assertEqual(detect_sandbox(), "flatpak")
        mock_exists.assert_called_once()


class WrapCommandTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=True)
    @patch.dict("os.environ", {}, clear=True)
    def test_wrap_flatpak(self, _mock_exists):
        cmd = ["/home/user/.venv/bin/python", "script.py"]
        result = wrap_command_for_sandbox(cmd)
        self.assertEqual(result, ["flatpak-spawn", "--host", "/home/user/.venv/bin/python", "script.py"])

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=False)
    @patch.dict("os.environ", {"SNAP_NAME": "libreoffice"}, clear=True)
    def test_wrap_snap_unchanged(self, _mock_exists):
        cmd = ["/home/user/.venv/bin/python", "script.py"]
        result = wrap_command_for_sandbox(cmd)
        self.assertEqual(result, cmd)

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=False)
    @patch.dict("os.environ", {}, clear=True)
    def test_wrap_no_sandbox(self, _mock_exists):
        cmd = ["/usr/bin/python3", "-c", "print('hello')"]
        result = wrap_command_for_sandbox(cmd)
        self.assertEqual(result, cmd)

    @patch("plugin.scripting.subprocess_helpers.os.path.exists", return_value=True)
    @patch.dict("os.environ", {}, clear=True)
    def test_wrap_does_not_mutate_original(self, _mock_exists):
        cmd = ["/usr/bin/python3", "script.py"]
        original = cmd.copy()
        wrap_command_for_sandbox(cmd)
        self.assertEqual(cmd, original)


class PipeOptimizeTests(unittest.TestCase):
    @patch("plugin.scripting.subprocess_helpers.sys.platform", "linux")
    @patch("fcntl.fcntl")
    def test_optimize_pipe_calls_fcntl(self, mock_fcntl: MagicMock) -> None:
        optimize_pipe(7)
        mock_fcntl.assert_called_once()
        args = mock_fcntl.call_args[0]
        self.assertEqual(args[0], 7)
        self.assertEqual(args[2], _PIPE_BUF_TARGET)

    @patch("plugin.scripting.subprocess_helpers.sys.platform", "linux")
    @patch("fcntl.fcntl", side_effect=OSError("cap denied"))
    def test_optimize_pipe_swallows_oserror(self, _mock_fcntl: MagicMock) -> None:
        optimize_pipe(3)

    @patch("plugin.scripting.subprocess_helpers.optimize_pipe")
    def test_optimize_popen_pipes_iterates_streams(self, mock_optimize: MagicMock) -> None:
        proc = MagicMock()
        proc.stdin.fileno.return_value = 10
        proc.stdout.fileno.return_value = 11
        proc.stderr.fileno.return_value = 12
        optimize_popen_pipes(proc)
        self.assertEqual(mock_optimize.call_count, 3)
        mock_optimize.assert_any_call(10)
        mock_optimize.assert_any_call(11)
        mock_optimize.assert_any_call(12)

    @patch("plugin.scripting.subprocess_helpers.optimize_pipe")
    def test_optimize_popen_pipes_skips_none_streams(self, mock_optimize: MagicMock) -> None:
        proc = MagicMock()
        proc.stdin = None
        proc.stdout.fileno.return_value = 11
        proc.stderr = None
        optimize_popen_pipes(proc)
        mock_optimize.assert_called_once_with(11)

    @patch("plugin.scripting.subprocess_helpers.sys.platform", "win32")
    @patch("fcntl.fcntl")
    def test_optimize_pipe_noop_on_windows(self, mock_fcntl: MagicMock) -> None:
        optimize_pipe(5)
        mock_fcntl.assert_not_called()

    @patch("plugin.scripting.subprocess_helpers.sys.platform", "darwin")
    @patch("fcntl.fcntl")
    def test_optimize_pipe_noop_on_macos(self, mock_fcntl: MagicMock) -> None:
        optimize_pipe(5)
        mock_fcntl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
