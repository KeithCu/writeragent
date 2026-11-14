# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any


class FakeShape:
    def __init__(self, shape_type: str, props: dict[str, Any] | None = None, text: str = "") -> None:
        self.shape_type = shape_type
        self.props = dict(props or {})
        self.sets: list[tuple[str, Any]] = []
        self.position = (1, 2)
        self.size = (10, 20)
        self.string = text

    def getShapeType(self) -> str:
        return self.shape_type

    def getPropertyValue(self, name: str) -> Any:
        if name not in self.props:
            raise AttributeError(name)
        return self.props[name]

    def setPropertyValue(self, name: str, value: Any) -> None:
        self.sets.append((name, value))
        self.props[name] = value

    def getPosition(self) -> tuple[int, int]:
        return self.position

    def setPosition(self, value: tuple[int, int]) -> None:
        self.position = value

    def getSize(self) -> tuple[int, int]:
        return self.size

    def setSize(self, value: tuple[int, int]) -> None:
        self.size = value

    def getString(self) -> str:
        return self.string

    def setString(self, value: str) -> None:
        self.string = value


class FakePage:
    def __init__(self, shapes: list[Any] | None = None) -> None:
        self.shapes = list(shapes or [])

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int) -> Any:
        return self.shapes[index]

    def add(self, shape: Any) -> None:
        self.shapes.append(shape)

    def remove(self, shape: Any) -> None:
        self.shapes.remove(shape)
