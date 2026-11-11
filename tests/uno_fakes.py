# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared test fakes and stand-ins for PyUNO runtime behaviour."""

from __future__ import annotations


class PyUnoSimulatedException(Exception):
    """Simulates a PyUNO exception whose __setattr__ delegates to a UNO struct.

    CPython generator context managers assign ``exc.__traceback__ = traceback``
    during unwinding in ``_GeneratorContextManager.__exit__``. In PyUNO, exception
    types route attribute assignments through ``_uno_struct__setattr__`` into the
    underlying UNO struct, which fails to convert the traceback object and raises
    a RuntimeError ("Couldn't convert <traceback object> to a UNO type").
    """

    def __setattr__(self, name: str, value: object) -> None:
        if name == "__traceback__":
            raise RuntimeError(
                f"Couldn't convert {value!r} to a UNO type; caught exception: <class 'AttributeError'>: 'traceback' object has no attribute 'getTypes'"
            )
        super().__setattr__(name, value)
