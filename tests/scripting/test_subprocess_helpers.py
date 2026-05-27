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
from unittest.mock import patch

from plugin.scripting.subprocess_helpers import _reset_cache, detect_sandbox, wrap_command_for_sandbox


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


if __name__ == "__main__":
    unittest.main()
