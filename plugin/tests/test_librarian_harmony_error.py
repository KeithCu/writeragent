# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import unittest

from plugin.framework.errors import NetworkError, user_message_if_provider_harmony_tool_parse_failure


class TestLibrarianHarmonyError(unittest.TestCase):
    def test_returns_hint_when_chain_has_parse_and_channel_tokens(self):
        inner = NetworkError(
            "HTTP Error 500 from AI Provider: Internal Server Error. "
            "Failed to parse input at pos 274: <|channel|>final <|constrain|>commentary<|message|>Action:"
        )

        class _AgentGenerationError(Exception):
            pass

        try:
            raise inner
        except NetworkError as cause:
            try:
                raise _AgentGenerationError(f"Error while generating output:\n{cause}") from cause
            except _AgentGenerationError as outer:
                msg = user_message_if_provider_harmony_tool_parse_failure(outer)
        self.assertIsNotNone(msg)
        self.assertIn("tool calls", msg.lower())
        self.assertIn("settings", msg.lower())

    def test_returns_none_for_unrelated_error(self):
        e = NetworkError("HTTP Error 404 from AI Provider: Not Found")
        self.assertIsNone(user_message_if_provider_harmony_tool_parse_failure(e))

    def test_returns_none_when_parse_error_without_channel_tokens(self):
        e = NetworkError("Failed to parse input at pos 1: unexpected token")
        self.assertIsNone(user_message_if_provider_harmony_tool_parse_failure(e))


if __name__ == "__main__":
    unittest.main()
