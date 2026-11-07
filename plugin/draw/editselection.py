# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extend/Edit Selection for Draw and Impress text shapes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from plugin.framework.send_cancellation import SendCancellation

log = logging.getLogger(__name__)

from plugin.chatbot.dialogs import msgbox
from plugin.chatbot.selection import (
    ApplyChunkFn,
    ErrorFn,
    StreamCompletionTask,
    bind_selection_cancel,
    create_validated_client,
    end_selection_cancel,
    prompt_for_edit_instructions,
    stream_completion_tasks,
)
from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.doc.visual_helpers import _controller_selection
from plugin.draw.draw_undo import register_draw_text_edit_undo
from plugin.draw.tables import _is_table_shape
from plugin.draw.tree import _is_group, _iter_children
from plugin.framework.config import get_config
from plugin.framework.errors import format_error_message, is_disposal
from plugin.framework.i18n import _
from plugin.writer.edit_review import build_writer_rewrite_prompt, strip_echoed_edit_scaffolding

_MAX_SHAPES = 250


def _is_text_edit_cursor(obj: Any) -> bool:
    """In-text edit selection from SdUnoDrawView when IsTextEdit() (SdUnoDrawView.cxx)."""
    return (
        hasattr(obj, "getString")
        and hasattr(obj, "getText")
        and hasattr(obj, "getStart")
        and hasattr(obj, "getEnd")
        and not hasattr(obj, "getByIndex")
    )


def _read_text(text_obj: Any) -> str | None:
    try:
        val = text_obj.getString()
    except Exception as exc:
        if is_disposal(exc):
            raise
        return None
    return None if val is None else str(val)


def _text_surfaces(shape: Any) -> list[Any]:
    """Objects that accept setString for this shape's text (shape and/or XText)."""
    surfaces: list[Any] = []
    if hasattr(shape, "getString") and hasattr(shape, "setString"):
        surfaces.append(shape)
    if hasattr(shape, "getText"):
        try:
            xtext = shape.getText()
        except Exception as exc:
            if is_disposal(exc):
                raise
            xtext = None
        if (
            xtext is not None
            and hasattr(xtext, "getString")
            and hasattr(xtext, "setString")
            and xtext not in surfaces
        ):
            surfaces.append(xtext)
    return surfaces


def _append_text_target(targets: list[Any], shape: Any) -> None:
    surfaces = _text_surfaces(shape)
    non_empty: list[tuple[Any, str]] = []
    for surface in surfaces:
        text = _read_text(surface)
        if text:
            non_empty.append((surface, text))
    if not non_empty:
        return
    # Prefer the shape object when it holds the same text; Impress undo stacks more reliably on the shape.
    for surface, _text in non_empty:
        if surface is shape:
            targets.append(surface)
            return
    targets.append(non_empty[0][0])


def _walk_shape_for_targets(shape: Any, targets: list[Any], saw_table: list[bool]) -> None:
    if _is_table_shape(shape):
        saw_table[0] = True
        return
    if _is_group(shape):
        for _child_idx, child in _iter_children(shape):
            _walk_shape_for_targets(child, targets, saw_table)
        return
    if _text_surfaces(shape):
        _append_text_target(targets, shape)


def iter_draw_rewrite_targets(selection: Any) -> tuple[list[Any], bool]:
    """Return (text objects with getString/setString, saw_table).

    ShapeCollection and a bare shape are both accepted.
    An XTextCursor (getString, getText, getStart, getEnd, and no getByIndex)
    contributes cursor.getText(), not the cursor.
    """
    targets: list[Any] = []
    saw_table = [False]
    if selection is None:
        return targets, False

    if _is_text_edit_cursor(selection):
        # SdUnoDrawView::getSelection uses SdrObjEditView::getTextSelection when IsTextEdit();
        # otherwise the marked shapes are returned as a ShapeCollection (SdUnoDrawView.cxx).
        # Partial highlight still rewrites the whole shape text (setString on XText replaces all text).
        text_obj = selection.getText()
        if text_obj is not None and hasattr(text_obj, "getString") and hasattr(text_obj, "setString"):
            _append_text_target(targets, text_obj)
        return targets, saw_table[0]

    if hasattr(selection, "getByIndex"):
        try:
            count = selection.getCount()
        except Exception as exc:
            if is_disposal(exc):
                raise
            return targets, saw_table[0]
        for i in range(count):
            try:
                shape = selection.getByIndex(i)
            except Exception as exc:
                if is_disposal(exc):
                    raise
                continue
            _walk_shape_for_targets(shape, targets, saw_table)
        return targets, saw_table[0]

    _walk_shape_for_targets(selection, targets, saw_table)
    return targets, saw_table[0]


def _disable_text_autogrow(text_obj: Any) -> None:
    for prop in ("TextAutoGrowHeight", "TextAutoGrowWidth"):
        try:
            text_obj.setPropertyValue(prop, False)
        except Exception as exc:
            if is_disposal(exc):
                raise


def _no_text_notice(saw_table: bool) -> str:
    if saw_table:
        return _("Select a text shape. Tables are not rewritten.")
    return _("Select a text shape.")


def do_draw_extend_edit(
    ctx: Any,
    model: Any,
    is_edit: bool,
    *,
    preset_instruction: str | None = None,
    sidebar_notify: Callable[[str], None] | None = None,
    allow_whole_document: bool = False,
    cancel: SendCancellation | None = None,
    host: Any | None = None,
) -> None:
    """Rewrite selected Draw/Impress text shapes in place via streaming completion.

    ``allow_whole_document`` is accepted for dispatch parity with Writer/Calc but unused:
    a slide is not one text body, so an empty selection never walks the page.
    """
    del allow_whole_document

    selection = _controller_selection(model)
    text_targets, saw_table = iter_draw_rewrite_targets(selection)

    user_input = ""
    extra_instructions = ""
    title = _("WriterAgent: Edit Selection (Draw)") if is_edit else _("WriterAgent: Extend Selection (Draw)")
    if is_edit:
        if preset_instruction is not None:
            user_input = preset_instruction
        else:
            edit_request = prompt_for_edit_instructions(ctx, title)
            if edit_request is None:
                return
            user_input, extra_instructions = edit_request

    extend_sys = get_config("extend_selection_system_prompt")
    extend_max = get_config("chatbot.extend_selection_max_tokens")
    edit_sys = extra_instructions or get_config("edit_selection_system_prompt")
    edit_max = get_config("chatbot.edit_selection_max_new_tokens")

    tasks: list[StreamCompletionTask] = []
    for text_obj in text_targets:
        if len(tasks) >= _MAX_SHAPES:
            break
        try:
            original = text_obj.getString()
        except Exception as exc:
            if is_disposal(exc):
                raise
            continue
        if not original:
            continue
        tasks.append(
            StreamCompletionTask(
                build_writer_rewrite_prompt(original, user_input) if is_edit else original,
                edit_sys if is_edit else extend_sys,
                len(original) + edit_max if is_edit else extend_max,
                (text_obj, original),
            )
        )

    if len(tasks) >= _MAX_SHAPES:
        msgbox(ctx, title, _("Only the first {0} non-empty cells are edited.").format(_MAX_SHAPES))

    if not tasks:
        notice = _no_text_notice(saw_table)
        if is_edit and preset_instruction is not None and sidebar_notify is not None:
            sidebar_notify(notice)
            return
        msgbox(ctx, title, notice)
        return

    client = create_validated_client(ctx, title)
    if client is None:
        return

    from plugin.doc.agent_undo import record_agent_turn_after, record_agent_turn_before

    record_agent_turn_before(model)

    completion_meta: dict[str, Any] = {}
    produced_text = [False]
    autogrow_done: set[int] = set()

    # Capture text objects and originals for custom undo action
    text_obj_originals: list[tuple[Any, str]] = []
    final_texts: list[str] = []
    for task in tasks:
        text_obj, original = task.payload
        text_obj_originals.append((text_obj, original))
        final_texts.append(original)  # Will be updated during streaming

    def prepare_task(task: StreamCompletionTask) -> tuple[ApplyChunkFn, ErrorFn]:
        text_obj, original = task.payload
        obj_id = id(text_obj)
        accumulated_text = [task.prompt if not is_edit else ""]
        task_idx = [i for i, (obj, _orig) in enumerate(text_obj_originals) if obj is text_obj][0]

        def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
            if not is_thinking:
                if chunk_text.strip():
                    produced_text[0] = True
                if obj_id not in autogrow_done:
                    autogrow_done.add(obj_id)
                    _disable_text_autogrow(text_obj)
                accumulated_text[0] += chunk_text
                final_text = strip_echoed_edit_scaffolding(accumulated_text[0])
                text_obj.setString(final_text)
                final_texts[task_idx] = final_text

        def on_error(e: BaseException) -> None:
            text_obj.setString(original)
            msgbox(ctx, title, format_error_message(e))

        return apply_chunk, on_error

    undo = WriterCompoundUndo(model, title)

    # Draw/Impress setString does not push child undo actions; attach our XUndoAction instead.
    try:
        register_draw_text_edit_undo(model, undo, text_obj_originals, final_texts, title)
    except ImportError:
        # UNO types unavailable (some unit-test environments); compound undo still groups if LO adds actions.
        log.debug("UNO not available; skipping custom draw text undo registration")
    except Exception as exc:
        from plugin.framework.errors import reraise_if_disposed

        reraise_if_disposed(exc)
        log.exception("Draw edit selection: custom undo registration failed")
        undo.close()
        record_agent_turn_after(model)
        msgbox(
            ctx,
            title,
            _("Could not register undo for this edit. The change was not applied."),
        )
        return

    def on_finished() -> None:
        try:
            undo.close()
            record_agent_turn_after(model)
            if (
                is_edit
                and preset_instruction is not None
                and sidebar_notify is not None
                and not produced_text[0]
            ):
                finish = completion_meta.get("finish_reason")
                if finish == "length":
                    sidebar_notify(
                        _("The rewrite ran out of tokens before producing text. Try again or shorten the selection.")
                    )
                elif finish == "stop":
                    sidebar_notify(_("The model returned no text. Try again."))
        finally:
            end_selection_cancel(arm)

    arm = bind_selection_cancel(host, model, cancel)
    try:
        stream_completion_tasks(
            ctx,
            client,
            tasks,
            prepare_task,
            on_finished,
            cancel=arm.scope,
            completion_out=completion_meta,
        )
    except BaseException:
        try:
            undo.close()
            record_agent_turn_after(model)
        finally:
            end_selection_cancel(arm)
        raise
