# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Unit tests for sidebar query Enter-to-send key classification."""

import unittest

from plugin.framework.config import _get_schema_default
from plugin.modules.chatbot.panel import query_enter_triggers_primary_send


class QueryEnterSendTests(unittest.TestCase):
    def test_enter_without_shift_triggers(self):
        self.assertTrue(query_enter_triggers_primary_send(1280, 0))

    def test_shift_enter_does_not_trigger(self):
        self.assertFalse(query_enter_triggers_primary_send(1280, 1))

    def test_shift_with_other_modifiers(self):
        self.assertFalse(query_enter_triggers_primary_send(1280, 1 | 2))

    def test_non_return_key_ignored(self):
        self.assertFalse(query_enter_triggers_primary_send(1279, 0))

    def test_doc_yaml_default_enter_sends_true(self):
        self.assertIs(_get_schema_default("doc.chat_enter_key_sends_message"), True)


if __name__ == "__main__":
    unittest.main()
