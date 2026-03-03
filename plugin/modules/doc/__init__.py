"""Common tools and diagnostics module."""

import logging
from plugin.framework.module_base import ModuleBase

log = logging.getLogger("localwriter.doc")


class CommonModule(ModuleBase):
    """Provides generic document tools and debug diagnostics."""

    def initialize(self, services):
        self._services = services

    def on_action(self, action):
        cfg = self._services.config.proxy_for(self.name)
        if action in ("debug_info", "test_providers"):
            if not cfg.get("debug_enabled"):
                from plugin.framework.uno_context import get_ctx
                from plugin.framework.dialogs import msgbox
                msgbox(get_ctx(), "Debug",
                       "Enable debug in Options > LocalWriter > Common")
                return

            if action == "debug_info":
                self._show_debug_info()
            elif action == "test_providers":
                self._test_providers()
        else:
            super().on_action(action)

    def _show_debug_info(self):
        """Show system info in a message box."""
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.dialogs import msgbox
        from plugin.version import EXTENSION_VERSION

        ctx = get_ctx()
        lines = ["LocalWriter v%s" % EXTENSION_VERSION, ""]

        # Registered services
        svc_names = sorted(self._services.list_services())
        lines.append("Services: %s" % ", ".join(svc_names))

        # AI instances
        ai = self._services.get("ai")
        if ai:
            lines.append("")
            lines.append("AI Instances:")
            for iid, inst in ai._instances.items():
                caps = ",".join(sorted(inst.capabilities))
                lines.append("  %s [%s]" % (iid, caps))
            active_text = ai.get_active_instance("text") or "(auto)"
            active_image = ai.get_active_instance("image") or "(auto)"
            lines.append("")
            lines.append("Active text: %s" % active_text)
            lines.append("Active image: %s" % active_image)

        # Document info
        doc_svc = self._services.get("document")
        if doc_svc:
            doc = doc_svc.get_active_document()
            if doc:
                doc_type = doc_svc.detect_doc_type(doc) or "unknown"
                doc_len = doc_svc.get_document_length(doc)
                lines.append("")
                lines.append("Document: %s (%d chars)" % (doc_type, doc_len))

        msgbox(ctx, "LocalWriter Debug Info", "\n".join(lines))

    def _test_providers(self):
        """Test each AI provider with a minimal request."""
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.dialogs import msgbox

        ctx = get_ctx()
        ai = self._services.get("ai")
        if not ai:
            msgbox(ctx, "Test Providers", "No AI service available")
            return

        lines = []
        for iid, inst in ai._instances.items():
            if "text" not in inst.capabilities:
                continue
            try:
                resp = inst.provider.complete(
                    [{"role": "user", "content": "Say OK"}],
                    max_tokens=5)
                content = (resp.get("content") or "")[:50]
                lines.append("[OK] %s: %s" % (iid, content))
            except Exception as e:
                lines.append("[FAIL] %s: %s" % (iid, str(e)[:80]))

        if not lines:
            lines.append("No text providers registered")

        msgbox(ctx, "Test AI Providers", "\n".join(lines))
