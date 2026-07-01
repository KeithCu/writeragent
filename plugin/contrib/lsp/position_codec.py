# WriterAgent - vendored from pygls v2.1.1 workspace/position_codec.py (Apache-2.0).
# lsprotocol types replaced with local ClientPosition; Harper uses UTF-16 only.
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ClientPosition:
    line: int
    character: int


@dataclass(order=True)
class ServerTextPosition:
    line: int
    character: int


class UnitCounter:
    def code_units_for_char(self, char: str) -> int:
        raise NotImplementedError

    def num_units(self, chars: str) -> int:
        return sum(self.code_units_for_char(c) for c in chars)


class Utf16(UnitCounter):
    def code_units_for_char(self, char: str) -> int:
        return 2 if ord(char) > 0xFFFF else 1


class PositionCodec:
    """Convert LSP client positions (UTF-16 code units) to Python string indices."""

    def __init__(self, encoding: str = "utf-16") -> None:
        self.encoding = encoding
        self._impl = Utf16()

    def position_from_client_units(self, lines: Sequence[str], position: ClientPosition) -> ServerTextPosition:
        if len(lines) == 0:
            return ServerTextPosition(0, 0)
        if position.line >= len(lines):
            return ServerTextPosition(len(lines) - 1, self._impl.num_units(lines[-1]))

        line_text = lines[position.line].replace("\r\n", "\n")
        client_len = self._impl.num_units(line_text)

        if client_len == 0:
            return ServerTextPosition(position.line, 0)

        target = position.character
        if target >= client_len:
            return ServerTextPosition(position.line, len(line_text))

        client_position = 0
        utf32_index = 0
        for char in line_text:
            if client_position >= target:
                break
            client_position += self._impl.code_units_for_char(char)
            utf32_index += 1

        if client_position < target:
            utf32_index = len(line_text)

        return ServerTextPosition(line=position.line, character=utf32_index)
