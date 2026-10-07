with open('plugin/chatbot/panel.py', 'r') as f:
    content = f.read()

target = '    def _on_grammar_status(self, **data: Any) -> None:\n        """Show native grammar proofreader progress in the sidebar status field."""\n'
replacement = target + '        if getattr(self, "_panel_teardown", False) or self.ctx is None:\n            return\n'
content = content.replace(target, replacement)

content = content.replace(
    '        except Exception as e:\n            log.debug("_on_grammar_status: post_to_main_thread failed: %s", e)\n            self._set_status(text)',
    '        except Exception as e:\n            log.debug("_on_grammar_status: post_to_main_thread failed: %s", e)\n            return'
)

with open('plugin/chatbot/panel.py', 'w') as f:
    f.write(content)
