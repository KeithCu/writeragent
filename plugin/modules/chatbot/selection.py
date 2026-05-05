"""Action handlers for extending and editing document selections."""

import logging
from plugin.framework.i18n import _

log = logging.getLogger("writeragent.chatbot.selection")

# ── Extend Selection ─────────────────────────────────────────────


def action_extend_selection(services):
    """Get document selection -> stream AI completion -> append to text."""
    from plugin.framework.uno_context import get_ctx
    from plugin.framework.dialogs import msgbox

    ctx = get_ctx()
    doc_svc = services.document
    doc = doc_svc.get_active_document()
    if not doc:
        msgbox(ctx, "WriterAgent", "No document open")
        return

    doc_type = doc_svc.detect_doc_type(doc)
    if doc_type == "writer":
        _extend_writer(services, ctx, doc)
    elif doc_type == "calc":
        _extend_calc(services, ctx, doc)
    else:
        msgbox(ctx, "WriterAgent", "Extend selection not supported for this document type")


def _extend_writer(services, ctx, doc):
    """Extend selection in a Writer document."""
    from plugin.framework.dialogs import msgbox
    from plugin.framework.async_stream import run_stream_async
    from plugin.framework.config import get_api_config
    from plugin.framework.document import (
        WriterCompoundUndo,
        get_string_without_tracked_deletions,
    )
    from plugin.modules.http.client import LlmClient

    try:
        selection = doc.CurrentController.getSelection()
        text_range = selection.getByIndex(0)
        selected_text = get_string_without_tracked_deletions(text_range)
    except Exception as e:
        from com.sun.star.lang import DisposedException
        from com.sun.star.uno import RuntimeException, Exception as UnoException

        if isinstance(e, (DisposedException, RuntimeException, UnoException)):
            log.debug("Failed to get Writer selection (likely disposed): %s", e)
        else:
            log.debug("No valid Writer selection found: %s", e)
        msgbox(ctx, "WriterAgent", "No text selected")
        return

    if not selected_text:
        msgbox(ctx, "WriterAgent", "No text selected")
        return

    config = services.config.proxy_for("chatbot")
    system_prompt = config.get("system_prompt") or ""
    _mt = config.get("extend_selection_max_tokens") or 70
    max_tokens = int(float(_mt))

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": selected_text})

    compound_undo = WriterCompoundUndo(doc, "WriterAgent: Extend selection")

    def apply_chunk(text, is_thinking=False):
        if not is_thinking:
            try:
                text_range.setString(text_range.getString() + text)
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException

                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("Failed to append text to Writer selection (likely disposed): %s", e)
                else:
                    log.exception("Failed to append text")

    def on_done():
        compound_undo.close()

    def on_error(e):
        try:
            log.error("Extend selection failed: %s", e)
            msgbox(ctx, _("WriterAgent: Extend Selection"), str(e))
        finally:
            compound_undo.close()

    api_config = get_api_config(ctx)
    client = LlmClient(api_config, ctx)
    run_stream_async(
        ctx,
        client,
        messages,
        tools=None,
        apply_chunk_fn=apply_chunk,
        on_done_fn=on_done,
        on_error_fn=on_error,
        max_tokens=max_tokens,
    )


def _extend_calc(services, ctx, doc):
    """Extend selection in a Calc document."""
    from plugin.framework.dialogs import msgbox
    from plugin.framework.async_stream import run_stream_async
    from plugin.framework.config import get_api_config
    from plugin.modules.http.client import LlmClient

    try:
        sheet = doc.CurrentController.ActiveSheet
        selection = doc.CurrentController.Selection
        area = selection.getRangeAddress()
    except Exception as e:
        from com.sun.star.lang import DisposedException
        from com.sun.star.uno import RuntimeException, Exception as UnoException

        if isinstance(e, (DisposedException, RuntimeException, UnoException)):
            log.debug("Failed to get Calc selection (likely disposed): %s", e)
        else:
            log.debug("No valid Calc selection found: %s", e)
        msgbox(ctx, "WriterAgent", "No cells selected")
        return

    config = services.config.proxy_for("chatbot")
    system_prompt = config.get("system_prompt") or ""
    _mt = config.get("extend_selection_max_tokens") or 70
    max_tokens = int(float(_mt))

    # Build task list
    tasks = []
    cell_range = sheet.getCellRangeByPosition(area.StartColumn, area.StartRow, area.EndColumn, area.EndRow)
    data_array = cell_range.getDataArray()

    for row_idx, row in enumerate(range(area.StartRow, area.EndRow + 1)):
        for col_idx, col in enumerate(range(area.StartColumn, area.EndColumn + 1)):
            raw_val = data_array[row_idx][col_idx]
            cell_text = str(raw_val) if raw_val != "" and raw_val is not None else ""

            if cell_text:
                cell = sheet.getCellByPosition(col, row)
                tasks.append((cell, cell_text))

    if not tasks:
        msgbox(ctx, "WriterAgent", "No cells with content selected")
        return

    api_config = get_api_config(ctx)
    client = LlmClient(api_config, ctx)

    # Process cells sequentially via callback chain
    task_index = [0]

    def run_next_cell():
        if task_index[0] >= len(tasks):
            return
        cell, cell_text = tasks[task_index[0]]
        task_index[0] += 1

        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": cell_text})

        def apply_chunk(text, is_thinking=False):
            if not is_thinking:
                try:
                    cell.setString(cell.getString() + text)
                except Exception as e:
                    from com.sun.star.lang import DisposedException
                    from com.sun.star.uno import RuntimeException, Exception as UnoException

                    if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                        log.debug("Failed to append text to Calc cell (likely disposed): %s", e)

        def on_error(e):
            log.error("Extend selection (calc) failed: %s", e)
            msgbox(ctx, _("WriterAgent: Extend Selection"), str(e))

        run_stream_async(
            ctx,
            client,
            msgs,
            tools=None,
            apply_chunk_fn=apply_chunk,
            on_done_fn=run_next_cell,
            on_error_fn=on_error,
            max_tokens=max_tokens,
        )

    run_next_cell()


# ── Edit Selection ───────────────────────────────────────────────


def action_edit_selection(services):
    """Get selection -> input instructions -> stream AI -> replace text."""
    from plugin.framework.uno_context import get_ctx
    from plugin.framework.dialogs import msgbox

    ctx = get_ctx()
    doc_svc = services.document
    doc = doc_svc.get_active_document()
    if not doc:
        msgbox(ctx, "WriterAgent", "No document open")
        return

    doc_type = doc_svc.detect_doc_type(doc)
    if doc_type == "writer":
        _edit_writer(services, ctx, doc)
    elif doc_type == "calc":
        _edit_calc(services, ctx, doc)
    else:
        msgbox(ctx, "WriterAgent", "Edit selection not supported for this document type")


def _show_edit_input():
    """Show the edit instructions dialog. Returns (user_input, extra_instructions); empty strings if cancelled.
    Uses the shared EditInputDialog.xdl (legacy_ui.input_box) so menu and shortcut share the same UI.
    """
    from plugin.framework.uno_context import get_ctx
    from plugin.framework.legacy_ui import input_box

    ctx = get_ctx()
    user_input, extra_instructions = input_box(ctx, "Please enter edit instructions!", "Input", "")
    return user_input, extra_instructions


def _edit_writer(services, ctx, doc):
    """Edit selection in a Writer document."""
    from plugin.framework.dialogs import msgbox
    from plugin.framework.async_stream import run_stream_async
    from plugin.framework.config import get_api_config
    from plugin.framework.document import (
        build_writer_rewrite_prompt,
        get_string_without_tracked_deletions,
        WriterStreamedRewriteSession,
    )
    from plugin.modules.http.client import LlmClient

    try:
        selection = doc.CurrentController.getSelection()
        text_range = selection.getByIndex(0)
        original_text = get_string_without_tracked_deletions(text_range)
    except Exception as e:
        from com.sun.star.lang import DisposedException
        from com.sun.star.uno import RuntimeException, Exception as UnoException

        if isinstance(e, (DisposedException, RuntimeException, UnoException)):
            log.debug("Failed to get Writer selection for edit (likely disposed): %s", e)
        else:
            log.debug("No valid Writer selection found for edit: %s", e)
        msgbox(ctx, "WriterAgent", "No text selected")
        return

    if not original_text:
        msgbox(ctx, "WriterAgent", "No text selected")
        return

    user_input, extra_instructions = _show_edit_input()
    if not user_input:
        return
    if extra_instructions:
        from plugin.framework.config import set_config, update_lru_history, get_current_endpoint

        set_config(ctx, "additional_instructions", extra_instructions)
        update_lru_history(ctx, extra_instructions, "prompt_lru", get_current_endpoint(ctx))

    config = services.config.proxy_for("chatbot")
    system_prompt = extra_instructions or config.get("system_prompt") or ""
    _mnt = config.get("edit_selection_max_new_tokens") or 0
    max_new_tokens = int(float(_mnt))

    prompt = build_writer_rewrite_prompt(original_text, user_input)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    max_tokens = len(original_text) + max_new_tokens

    session = WriterStreamedRewriteSession(doc, text_range, original_text)

    def apply_chunk(text, is_thinking=False):
        if not is_thinking:
            session.append_chunk(text)

    def on_done():
        warning = session.finish()
        if warning:
            log.warning("Writer streamed rewrite fallback: %s", warning)
            msgbox(ctx, _("WriterAgent: Edit Selection"), warning)

    def on_error(e):
        try:
            session.abort_and_restore()
        except Exception as recovery_err:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException

            if isinstance(recovery_err, (DisposedException, RuntimeException, UnoException)):
                log.debug("Failed to restore original text (likely disposed): %s", recovery_err)
        log.error("Edit selection failed: %s", e)
        msgbox(ctx, _("WriterAgent: Edit Selection"), str(e))

    api_config = get_api_config(ctx)
    client = LlmClient(api_config, ctx)
    run_stream_async(
        ctx,
        client,
        messages,
        tools=None,
        apply_chunk_fn=apply_chunk,
        on_done_fn=on_done,
        on_error_fn=on_error,
        max_tokens=max_tokens,
    )


def _edit_calc(services, ctx, doc):
    """Edit selection in a Calc document."""
    from plugin.framework.dialogs import msgbox
    from plugin.framework.async_stream import run_stream_async
    from plugin.framework.config import get_api_config
    from plugin.modules.http.client import LlmClient

    try:
        sheet = doc.CurrentController.ActiveSheet
        selection = doc.CurrentController.Selection
        area = selection.getRangeAddress()
    except Exception as e:
        from com.sun.star.lang import DisposedException
        from com.sun.star.uno import RuntimeException, Exception as UnoException

        if isinstance(e, (DisposedException, RuntimeException, UnoException)):
            log.debug("Failed to get Calc selection for edit (likely disposed): %s", e)
        else:
            log.debug("No valid Calc selection found for edit: %s", e)
        msgbox(ctx, "WriterAgent", "No cells selected")
        return

    user_input, extra_instructions = _show_edit_input()
    if not user_input:
        return
    if extra_instructions:
        from plugin.framework.config import set_config, update_lru_history, get_current_endpoint

        set_config(ctx, "additional_instructions", extra_instructions)
        update_lru_history(ctx, extra_instructions, "prompt_lru", get_current_endpoint(ctx))

    config = services.config.proxy_for("chatbot")
    system_prompt = extra_instructions or config.get("system_prompt") or ""
    _mnt = config.get("edit_selection_max_new_tokens") or 0
    max_new_tokens = int(float(_mnt))

    # Build task list
    tasks = []
    cell_range = sheet.getCellRangeByPosition(area.StartColumn, area.StartRow, area.EndColumn, area.EndRow)
    data_array = cell_range.getDataArray()

    for row_idx, row in enumerate(range(area.StartRow, area.EndRow + 1)):
        for col_idx, col in enumerate(range(area.StartColumn, area.EndColumn + 1)):
            raw_val = data_array[row_idx][col_idx]
            original = str(raw_val) if raw_val != "" and raw_val is not None else ""

            prompt = (
                "ORIGINAL VERSION:\n" + original + "\n Below is an edited version according to the following "
                "instructions. Don't waste time thinking, be as fast as "
                "you can. The edited text will be a shorter or longer "
                "version of the original text based on the instructions. "
                "There are no comments in the edited version. The edited "
                "version is followed by the end of the document. The "
                "original version will be edited as follows to create "
                "the edited version:\n" + user_input + "\nEDITED VERSION:\n"
            )
            max_tokens = len(original) + max_new_tokens

            cell = sheet.getCellByPosition(col, row)
            tasks.append((cell, prompt, max_tokens, original))

    if not tasks:
        return

    api_config = get_api_config(ctx)
    client = LlmClient(api_config, ctx)

    # Process cells sequentially
    task_index = [0]

    def run_next_cell():
        if task_index[0] >= len(tasks):
            return
        cell, prompt, max_tok, original = tasks[task_index[0]]
        task_index[0] += 1

        cell.setString("")

        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": prompt})

        def apply_chunk(text, is_thinking=False):
            if not is_thinking:
                try:
                    cell.setString(cell.getString() + text)
                except Exception as e:
                    from com.sun.star.lang import DisposedException
                    from com.sun.star.uno import RuntimeException, Exception as UnoException

                    if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                        log.debug("Failed to write text to Calc cell (likely disposed): %s", e)

        def on_error(e):
            try:
                cell.setString(original)
            except Exception as recovery_err:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException

                if isinstance(recovery_err, (DisposedException, RuntimeException, UnoException)):
                    log.debug("Failed to restore original cell text (likely disposed): %s", recovery_err)
            log.error("Edit selection (calc) failed: %s", e)
            msgbox(ctx, _("WriterAgent: Edit Selection"), str(e))

        run_stream_async(
            ctx,
            client,
            msgs,
            tools=None,
            apply_chunk_fn=apply_chunk,
            on_done_fn=run_next_cell,
            on_error_fn=on_error,
            max_tokens=max_tok,
        )

    run_next_cell()
