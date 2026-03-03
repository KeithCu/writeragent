"""AI chat sidebar module."""

import logging

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("localwriter.chatbot")


class ChatbotModule(ModuleBase):
    """Registers the chatbot sidebar and its tool adapter."""

    def initialize(self, services):
        self._services = services
        self._routes_registered = False

        # Create the tool adapter for routing chat tool calls
        from plugin.modules.chatbot.panel import ChatToolAdapter
        self._adapter = ChatToolAdapter(services.tools, services)

        # Register API routes if enabled
        cfg = services.config.proxy_for(self.name)
        if cfg.get("api_enabled"):
            self._register_routes(services)

        services.events.subscribe(
            "config:changed", self._on_config_changed)

    def _on_config_changed(self, **kwargs):
        key = kwargs.get("key", "")
        if key != "chatbot.api_enabled":
            return
        enabled = kwargs.get("value")
        if enabled and not self._routes_registered:
            self._register_routes(self._services)
        elif not enabled and self._routes_registered:
            self._unregister_routes(self._services)

    def _register_routes(self, services):
        routes = services.get("http_routes")
        if not routes:
            log.warning("http_routes service not available")
            return

        from plugin.modules.chatbot.handler import ChatApiHandler
        self._api_handler = ChatApiHandler(services)

        routes.add("POST", "/api/chat",
                    self._api_handler.handle_chat, raw=True)
        routes.add("GET", "/api/chat",
                    self._api_handler.handle_history)
        routes.add("DELETE", "/api/chat",
                    self._api_handler.handle_reset)
        routes.add("GET", "/api/providers",
                    self._api_handler.handle_providers)

        self._routes_registered = True
        log.info("Chat API routes registered")

    def _unregister_routes(self, services):
        routes = services.get("http_routes")
        if routes:
            for method, path in [
                ("POST", "/api/chat"),
                ("GET", "/api/chat"),
                ("DELETE", "/api/chat"),
                ("GET", "/api/providers"),
            ]:
                try:
                    routes.remove(method, path)
                except Exception:
                    pass
        self._routes_registered = False
        log.info("Chat API routes unregistered")

    def get_adapter(self):
        """Return the ChatToolAdapter for use by the panel factory."""
        return self._adapter

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action):
        if action == "extend_selection":
            self._action_extend_selection()
        elif action == "edit_selection":
            self._action_edit_selection()
        else:
            super().on_action(action)

    # ── Extend Selection ─────────────────────────────────────────────

    def _action_extend_selection(self):
        """Get document selection -> stream AI completion -> append to text."""
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.dialogs import msgbox

        ctx = get_ctx()
        doc_svc = self._services.document
        doc = doc_svc.get_active_document()
        if not doc:
            msgbox(ctx, "LocalWriter", "No document open")
            return

        try:
            provider = self._services.ai.get_provider("text")
        except RuntimeError as e:
            msgbox(ctx, "LocalWriter", str(e))
            return

        doc_type = doc_svc.detect_doc_type(doc)
        if doc_type == "writer":
            self._extend_writer(ctx, doc, provider)
        elif doc_type == "calc":
            self._extend_calc(ctx, doc, provider)
        else:
            msgbox(ctx, "LocalWriter",
                   "Extend selection not supported for this document type")

    def _extend_writer(self, ctx, doc, provider):
        """Extend selection in a Writer document."""
        from plugin.framework.dialogs import msgbox
        from plugin.modules.chatbot.streaming import run_stream_async

        try:
            selection = doc.CurrentController.getSelection()
            text_range = selection.getByIndex(0)
            selected_text = text_range.getString()
        except Exception:
            msgbox(ctx, "LocalWriter", "No text selected")
            return

        if not selected_text:
            msgbox(ctx, "LocalWriter", "No text selected")
            return

        config = self._services.config.proxy_for("chatbot")
        system_prompt = config.get("system_prompt") or ""
        max_tokens = config.get("extend_selection_max_tokens") or 70

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": selected_text})

        def apply_chunk(text, is_thinking=False):
            if not is_thinking:
                try:
                    text_range.setString(text_range.getString() + text)
                except Exception:
                    log.exception("Failed to append text")

        def on_error(e):
            log.error("Extend selection failed: %s", e)
            msgbox(ctx, "LocalWriter: Extend Selection", str(e))

        run_stream_async(
            ctx, provider, messages, tools=None,
            apply_chunk_fn=apply_chunk,
            on_done_fn=lambda: None,
            on_error_fn=on_error,
            max_tokens=max_tokens,
        )

    def _extend_calc(self, ctx, doc, provider):
        """Extend selection in a Calc document."""
        from plugin.framework.dialogs import msgbox
        from plugin.modules.chatbot.streaming import run_stream_async

        try:
            sheet = doc.CurrentController.ActiveSheet
            selection = doc.CurrentController.Selection
            area = selection.getRangeAddress()
        except Exception:
            msgbox(ctx, "LocalWriter", "No cells selected")
            return

        config = self._services.config.proxy_for("chatbot")
        system_prompt = config.get("system_prompt") or ""
        max_tokens = config.get("extend_selection_max_tokens") or 70

        # Build task list
        tasks = []
        for row in range(area.StartRow, area.EndRow + 1):
            for col in range(area.StartColumn, area.EndColumn + 1):
                cell = sheet.getCellByPosition(col, row)
                cell_text = cell.getString()
                if cell_text:
                    tasks.append((cell, cell_text))

        if not tasks:
            msgbox(ctx, "LocalWriter", "No cells with content selected")
            return

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
                    except Exception:
                        pass

            def on_error(e):
                log.error("Extend selection (calc) failed: %s", e)
                msgbox(ctx, "LocalWriter: Extend Selection", str(e))

            run_stream_async(
                ctx, provider, msgs, tools=None,
                apply_chunk_fn=apply_chunk,
                on_done_fn=run_next_cell,
                on_error_fn=on_error,
                max_tokens=max_tokens,
            )

        run_next_cell()

    # ── Edit Selection ───────────────────────────────────────────────

    def _action_edit_selection(self):
        """Get selection -> input instructions -> stream AI -> replace text."""
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.dialogs import msgbox

        ctx = get_ctx()
        doc_svc = self._services.document
        doc = doc_svc.get_active_document()
        if not doc:
            msgbox(ctx, "LocalWriter", "No document open")
            return

        try:
            provider = self._services.ai.get_provider("text")
        except RuntimeError as e:
            msgbox(ctx, "LocalWriter", str(e))
            return

        doc_type = doc_svc.detect_doc_type(doc)
        if doc_type == "writer":
            self._edit_writer(ctx, doc, provider)
        elif doc_type == "calc":
            self._edit_calc(ctx, doc, provider)
        else:
            msgbox(ctx, "LocalWriter",
                   "Edit selection not supported for this document type")

    def _show_edit_input(self):
        """Show the edit instructions dialog. Returns user input or empty."""
        try:
            dlg = self.load_dialog("edit_input")
        except Exception:
            log.exception("Failed to load edit_input dialog")
            return ""

        try:
            import uno
            ctrl = dlg.getControl("edit")
            ctrl.setFocus()
            ctrl.setSelection(
                uno.createUnoStruct("com.sun.star.awt.Selection", 0, 0))
            if dlg.execute():
                return (ctrl.getModel().Text or "").strip()
            return ""
        finally:
            dlg.dispose()

    def _edit_writer(self, ctx, doc, provider):
        """Edit selection in a Writer document."""
        from plugin.framework.dialogs import msgbox
        from plugin.modules.chatbot.streaming import run_stream_async

        try:
            selection = doc.CurrentController.getSelection()
            text_range = selection.getByIndex(0)
            original_text = text_range.getString()
        except Exception:
            msgbox(ctx, "LocalWriter", "No text selected")
            return

        if not original_text:
            msgbox(ctx, "LocalWriter", "No text selected")
            return

        user_input = self._show_edit_input()
        if not user_input:
            return

        config = self._services.config.proxy_for("chatbot")
        system_prompt = config.get("system_prompt") or ""
        max_new_tokens = config.get("edit_selection_max_new_tokens") or 0

        prompt = (
            "ORIGINAL VERSION:\n" + original_text +
            "\n Below is an edited version according to the following "
            "instructions. There are no comments in the edited version. "
            "The edited version is followed by the end of the document. "
            "The original version will be edited as follows to create "
            "the edited version:\n" + user_input + "\nEDITED VERSION:\n"
        )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        max_tokens = len(original_text) + max_new_tokens

        # Clear selection and start streaming replacement
        text_range.setString("")

        def apply_chunk(text, is_thinking=False):
            if not is_thinking:
                try:
                    text_range.setString(text_range.getString() + text)
                except Exception:
                    log.exception("Failed to write text")

        def on_error(e):
            try:
                text_range.setString(original_text)
            except Exception:
                pass
            log.error("Edit selection failed: %s", e)
            msgbox(ctx, "LocalWriter: Edit Selection", str(e))

        run_stream_async(
            ctx, provider, messages, tools=None,
            apply_chunk_fn=apply_chunk,
            on_done_fn=lambda: None,
            on_error_fn=on_error,
            max_tokens=max_tokens,
        )

    def _edit_calc(self, ctx, doc, provider):
        """Edit selection in a Calc document."""
        from plugin.framework.dialogs import msgbox
        from plugin.modules.chatbot.streaming import run_stream_async

        try:
            sheet = doc.CurrentController.ActiveSheet
            selection = doc.CurrentController.Selection
            area = selection.getRangeAddress()
        except Exception:
            msgbox(ctx, "LocalWriter", "No cells selected")
            return

        user_input = self._show_edit_input()
        if not user_input:
            return

        config = self._services.config.proxy_for("chatbot")
        system_prompt = config.get("system_prompt") or ""
        max_new_tokens = config.get("edit_selection_max_new_tokens") or 0

        # Build task list
        tasks = []
        for row in range(area.StartRow, area.EndRow + 1):
            for col in range(area.StartColumn, area.EndColumn + 1):
                cell = sheet.getCellByPosition(col, row)
                original = cell.getString()
                prompt = (
                    "ORIGINAL VERSION:\n" + original +
                    "\n Below is an edited version according to the following "
                    "instructions. Don't waste time thinking, be as fast as "
                    "you can. The edited text will be a shorter or longer "
                    "version of the original text based on the instructions. "
                    "There are no comments in the edited version. The edited "
                    "version is followed by the end of the document. The "
                    "original version will be edited as follows to create "
                    "the edited version:\n" + user_input +
                    "\nEDITED VERSION:\n"
                )
                max_tokens = len(original) + max_new_tokens
                tasks.append((cell, prompt, max_tokens, original))

        if not tasks:
            return

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
                    except Exception:
                        pass

            def on_error(e):
                try:
                    cell.setString(original)
                except Exception:
                    pass
                log.error("Edit selection (calc) failed: %s", e)
                msgbox(ctx, "LocalWriter: Edit Selection", str(e))

            run_stream_async(
                ctx, provider, msgs, tools=None,
                apply_chunk_fn=apply_chunk,
                on_done_fn=run_next_cell,
                on_error_fn=on_error,
                max_tokens=max_tok,
            )

        run_next_cell()
