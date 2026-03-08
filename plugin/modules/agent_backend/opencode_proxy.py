"""OpenCode agent backend adapter. Wraps the OpenCode CLI in a long-lived process."""

from plugin.modules.agent_backend.cli_backend import CLIProcessBackend, strip_ansi


class OpenCodeBackend(CLIProcessBackend):
    backend_id = "opencode"
    display_name = "OpenCode"

    def get_default_cmd(self):
        return "opencode"

    def is_ready_prompt(self, line):
        if not line:
            return False
        s = strip_ansi(line).strip()
        # Similar heuristic: watch for trailing ">" or "Enter command:"
        return s.endswith(">") or "Enter prompt" in s

    def is_end_of_response(self, line):
        return self.is_ready_prompt(line)

    def format_input(self, user_message, document_context, document_url, system_prompt, selection_text, **kwargs):
        parts = []
        if system_prompt:
            parts.append(system_prompt)
            parts.append("\n\n")
        if document_context:
            parts.append("Context:\n")
            parts.append(document_context)
            parts.append("\n\n")
        parts.append(user_message)
        parts.append("\n")
        return "".join(parts)
