from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.i18n import _


@pytest.fixture(autouse=True)
def _restore_drain_idle_callbacks():
    from plugin.framework import async_drain_guard as guard

    saved = list(guard._drain_idle_callbacks)
    try:
        yield
    finally:
        with guard._drain_lock:
            guard._drain_idle_callbacks[:] = [
                cb for cb in guard._drain_idle_callbacks
                if cb in saved or getattr(cb, "__module__", None) != "plugin.chatbot.selection"
            ]


def _config(key):
    return {
        "extend_selection_system_prompt": "extend system",
        "edit_selection_system_prompt": "edit system",
        "chatbot.extend_selection_max_tokens": 50,
        "chatbot.edit_selection_max_new_tokens": 7,
    }.get(key, 0 if "max" in key else "")


class _TextShape:
    def __init__(self, text: str, *, shape_type: str = "TextShape") -> None:
        self._text = text
        self._shape_type = shape_type
        self.autogrow_calls: list[tuple[str, bool]] = []

    def getString(self) -> str:
        return self._text

    def setString(self, value: str) -> None:
        self._text = value

    def getShapeType(self) -> str:
        return self._shape_type

    def setPropertyValue(self, name: str, value: object) -> None:
        self.autogrow_calls.append((name, bool(value)))


class _GraphicShape:
    def getShapeType(self) -> str:
        return "GraphicObjectShape"

    def getString(self) -> str:
        raise RuntimeError("no text")


class _GroupShape:
    def __init__(self, children: list) -> None:
        self._children = children

    def getShapeType(self) -> str:
        return "GroupShape"

    def getCount(self) -> int:
        return len(self._children)

    def getByIndex(self, i: int):
        return self._children[i]


class _ShapeCollection:
    def __init__(self, shapes: list) -> None:
        self._shapes = shapes

    def getCount(self) -> int:
        return len(self._shapes)

    def getByIndex(self, i: int):
        return self._shapes[i]


class _TableShape:
    def getShapeType(self) -> str:
        return "TableShape"

    def getString(self) -> str:
        return "table"

    def setString(self, value: str) -> None:
        raise AssertionError("table setString should not run")


class _UndoManager:
    def __init__(self) -> None:
        self.open = False
        self.enters: list[str] = []
        self.leaves = 0

    def getAllUndoActionTitles(self) -> list[str]:
        return []

    def isLocked(self) -> bool:
        return False

    def isInContext(self) -> bool:
        return self.open

    def isUndoEnabled(self) -> bool:
        return True

    def enterUndoContext(self, title: str) -> None:
        self.open = True
        self.enters.append(title)

    def leaveUndoContext(self) -> None:
        self.open = False
        self.leaves += 1

    def addUndoAction(self, action: object) -> None:
        del action


@contextmanager
def _draw_model(selection):
    model = MagicMock()
    um = _UndoManager()
    model.getUndoManager.return_value = um
    with patch("plugin.draw.editselection._controller_selection", return_value=selection):
        yield model, um


def _pump_idle() -> None:
    from plugin.framework.async_drain_guard import _notify_drain_idle

    for unused in range(6):
        _notify_drain_idle()


def test_draw_edit_one_shape_prompt_undo_and_agent_turns():
    from plugin.draw.editselection import do_draw_extend_edit

    shape = _TextShape("Hello")
    stream_calls = []
    before = MagicMock()
    after = MagicMock()

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        apply_chunk_fn("Hi", False)
        on_done_fn()

    with _draw_model(_ShapeCollection([shape])) as (model, um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream), \
         patch("plugin.doc.agent_undo.record_agent_turn_before", before), \
         patch("plugin.doc.agent_undo.record_agent_turn_after", after):
        do_draw_extend_edit(MagicMock(), model, is_edit=True, preset_instruction="Fix spelling.")
        _pump_idle()

    assert "Text to rewrite:\nHello" in stream_calls[0]
    assert shape.getString() == "Hi"
    assert um.enters == [_("WriterAgent: Edit Selection (Draw)")]
    assert um.leaves == 1
    before.assert_called_once_with(model)
    after.assert_called_once_with(model)


def test_draw_extend_appends_to_original():
    from plugin.draw.editselection import do_draw_extend_edit

    shape = _TextShape("Hello")

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        apply_chunk_fn(" world", False)
        on_done_fn()

    with _draw_model(_ShapeCollection([shape])) as (model, unused_um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=False)

    assert shape.getString() == "Hello world"


def test_draw_two_shapes_two_streams_one_undo():
    from plugin.draw.editselection import do_draw_extend_edit

    first = _TextShape("A")
    second = _TextShape("B")
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        apply_chunk_fn("!", False)
        on_done_fn()

    with _draw_model(_ShapeCollection([first, second])) as (model, um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=False)
        _pump_idle()

    assert stream_calls == ["A", "B"]
    assert um.leaves == 1


def test_draw_skips_empty_and_graphic_getstring_failure():
    from plugin.draw.editselection import do_draw_extend_edit

    empty = _TextShape("")
    graphic = _GraphicShape()
    good = _TextShape("Ok")
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        on_done_fn()

    with _draw_model(_ShapeCollection([empty, graphic, good])) as (model, unused_um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=False)

    assert stream_calls == ["Ok"]


def test_draw_no_selection_allow_whole_document_notifies_sidebar():
    from plugin.draw.editselection import do_draw_extend_edit

    model = MagicMock()
    sidebar: list[str] = []
    run = MagicMock()

    with patch("plugin.draw.editselection._controller_selection", return_value=None), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.draw.editselection.stream_completion_tasks", run):
        do_draw_extend_edit(
            MagicMock(),
            model,
            is_edit=True,
            preset_instruction="proofread",
            sidebar_notify=sidebar.append,
            allow_whole_document=True,
        )

    assert sidebar == [_("Select a text shape.")]
    run.assert_not_called()


def test_draw_no_selection_without_sidebar_msgbox():
    from plugin.draw.editselection import do_draw_extend_edit

    model = MagicMock()

    with patch("plugin.draw.editselection._controller_selection", return_value=None), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.msgbox") as msgbox:
        do_draw_extend_edit(MagicMock(), model, is_edit=False)

    msgbox.assert_called_once()
    assert msgbox.call_args[0][2] == _("Select a text shape.")


def test_draw_group_rewrites_children_not_group():
    from plugin.draw.editselection import do_draw_extend_edit

    child1 = _TextShape("one")
    child2 = _TextShape("two")
    group = _GroupShape([child1, child2])
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        apply_chunk_fn("!", False)
        on_done_fn()

    with _draw_model(_ShapeCollection([group])) as (model, unused_um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=False)
        _pump_idle()

    assert stream_calls == ["one", "two"]
    assert not hasattr(group, "setString")


def test_draw_table_only_no_stream_table_notice():
    from plugin.draw.editselection import do_draw_extend_edit

    table = _TableShape()
    sidebar: list[str] = []

    with _draw_model(_ShapeCollection([table])) as (model, unused_um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.draw.editselection.stream_completion_tasks") as run:
        do_draw_extend_edit(
            MagicMock(),
            model,
            is_edit=True,
            preset_instruction="proofread",
            sidebar_notify=sidebar.append,
        )

    assert sidebar == [_("Select a text shape. Tables are not rewritten.")]
    run.assert_not_called()


def test_draw_table_plus_text_rewrites_text_only():
    from plugin.draw.editselection import do_draw_extend_edit

    table = _TableShape()
    text = _TextShape("Hello")
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        apply_chunk_fn("Hi", False)
        on_done_fn()

    with _draw_model(_ShapeCollection([table, text])) as (model, unused_um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=True, preset_instruction="proofread")

    assert len(stream_calls) == 1
    assert "Text to rewrite:\nHello" in stream_calls[0]


def test_draw_text_cursor_rewrites_whole_shape_text():
    from plugin.draw.editselection import do_draw_extend_edit, iter_draw_rewrite_targets

    shape = _TextShape("Hello world")

    class _Cursor:
        def getString(self) -> str:
            return "wor"

        def getText(self):
            return shape

        def getStart(self):
            return 0

        def getEnd(self):
            return 3

    cursor = _Cursor()
    targets, saw_table = iter_draw_rewrite_targets(cursor)
    assert saw_table is False
    assert targets == [shape]

    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        apply_chunk_fn("Hi", False)
        on_done_fn()

    with _draw_model(cursor) as (model, unused_um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=True, preset_instruction="proofread")

    assert "Text to rewrite:\nHello world" in stream_calls[0]
    assert shape.getString() == "Hi"


def test_draw_second_shape_error_restores_and_undo_closes():
    from plugin.draw.editselection import do_draw_extend_edit

    first = _TextShape("one")
    second = _TextShape("two")
    stream_idx = [0]

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        idx = stream_idx[0]
        stream_idx[0] += 1
        if idx == 0:
            apply_chunk_fn("ONE", False)
            on_done_fn()
        else:
            apply_chunk_fn("TWO", False)
            on_error_fn(RuntimeError("fail"))

    with _draw_model(_ShapeCollection([first, second])) as (model, um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.draw.editselection.msgbox") as msgbox, \
         patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=True, preset_instruction="proofread")
        _pump_idle()

    assert first.getString() == "ONE"
    assert second.getString() == "two"
    assert um.leaves == 1
    msgbox.assert_called_once()


def test_draw_max_shapes_cap_msgbox():
    from plugin.draw import editselection as mod
    from plugin.draw.editselection import do_draw_extend_edit

    shapes = [_TextShape(str(i)) for i in range(3)]
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        on_done_fn()

    with _draw_model(_ShapeCollection(shapes)) as (model, unused_um), \
         patch.object(mod, "_MAX_SHAPES", 2), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.draw.editselection.msgbox") as msgbox, \
         patch("plugin.framework.queue_executor.main.post", side_effect=lambda fn: fn()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=False)
        _pump_idle()

    assert len(stream_calls) == 2
    msgbox.assert_called_once()


def test_draw_whitespace_only_shape_is_rewritten():
    from plugin.draw.editselection import do_draw_extend_edit

    ws = _TextShape("   ")
    stream_calls = []

    def fake_run_stream(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, stop_checker=None, **_unused):
        stream_calls.append(prompt)
        on_done_fn()

    with _draw_model(_ShapeCollection([ws])) as (model, unused_um), \
         patch("plugin.draw.editselection.get_config", side_effect=_config), \
         patch("plugin.draw.editselection.create_validated_client", return_value=object()), \
         patch("plugin.chatbot.selection.run_stream_completion_async", side_effect=fake_run_stream):
        do_draw_extend_edit(MagicMock(), model, is_edit=False)

    assert stream_calls == ["   "]
