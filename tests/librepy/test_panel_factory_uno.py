# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for PythonPanelFactory XUIElementFactory exceptions."""

from __future__ import annotations

from typing import Any
from unittest import TestCase

from com.sun.star.container import NoSuchElementException
from com.sun.star.lang import IllegalArgumentException

from plugin.librepy.panel_factory import PythonPanelFactory
from plugin.testing_runner import native_test


@native_test
def test_create_ui_element_declared_exceptions_uno(ctx: Any) -> None:
    """soffice receives the exception types XUIElementFactory.createUIElement declares."""
    factory = PythonPanelFactory(ctx)
    # Invocation lives in soffice, so each call goes out over URP and back into the factory.
    invocation = ctx.getServiceManager().createInstanceWithContext("com.sun.star.script.Invocation", ctx)
    remote = invocation.createInstanceWithArguments((factory,))
    with TestCase().assertRaises(NoSuchElementException) as unknown:
        remote.createUIElement("private:resource/toolpanel/Other", ())
    assert unknown.exception.Message == "Unknown resource: private:resource/toolpanel/Other"
    assert unknown.exception.Context == factory
    with TestCase().assertRaises(IllegalArgumentException) as no_parent:
        remote.createUIElement("private:resource/toolpanel/PythonPanel", ())
    assert no_parent.exception.Message == "ParentWindow is required"
    assert no_parent.exception.Context == factory
    assert no_parent.exception.ArgumentPosition == 1
