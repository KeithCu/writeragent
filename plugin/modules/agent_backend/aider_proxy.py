"""Aider agent backend adapter. Wraps the Aider CLI in a long-lived process."""

from plugin.modules.agent_backend.cli_backend import CLIProcessBackend, strip_ansi


class AiderBackend(CLIProcessBackend):
    backend_id = "aider"
    display_name = "Aider"

    def get_default_cmd(self):
        return "aider"

    def is_ready_prompt(self, line):
        # Aider usually shows something like "> " or "aider> "
        if not line:
            return False
        s = strip_ansi(line).strip()
        return s.endswith(">")

    def is_end_of_response(self, line):
        return self.is_ready_prompt(line)

    def format_input(self, user_message, document_context, document_url, system_prompt, selection_text, **kwargs):
        # Aider has specific slash commands and expects typical conversational input.
        # It's primarily for coding, but we can pass it arbitrary prompts.
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
