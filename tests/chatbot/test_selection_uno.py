# WriterAgent - Native UNO Test for selection cancellation on document dispose
from __future__ import annotations

from typing import Any
import uno

from plugin.testing_runner import native_test
from plugin.framework.uno_context import get_desktop
from plugin.chatbot.selection import bind_selection_cancel, end_selection_cancel


@native_test
def test_bind_selection_cancel_cancels_on_document_close(ctx: Any) -> None:
    hidden = uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True)
    doc = get_desktop(ctx).loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden,))
    arm = bind_selection_cancel(None, doc)
    try:
        assert not arm.scope.is_cancelled()
        doc.close(True)
        assert arm.scope.is_cancelled()
    finally:
        end_selection_cancel(arm)
