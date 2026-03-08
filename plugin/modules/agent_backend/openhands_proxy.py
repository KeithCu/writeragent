"""OpenHands agent backend adapter. Wraps the OpenHands CLI in a long-lived process."""

from plugin.modules.agent_backend.cli_backend import CLIProcessBackend, strip_ansi


class OpenHandsBackend(CLIProcessBackend):
    backend_id = "openhands"
    display_name = "OpenHands"

    def get_default_cmd(self):
        return "openhands"

    def is_ready_prompt(self, line):
        # Depending on how OpenHands exposes its interactive CLI, we watch for its prompt.
        if not line:
            return False
        s = strip_ansi(line).strip()
        # Common pattern: "User> " or "OpenHands> "
        return s.endswith(">") or "Please enter your message" in s

    def is_end_of_response(self, line):
        return self.is_ready_prompt(line)

    def format_input(self, user_message, document_context, document_url, system_prompt, selection_text, **kwargs):
        parts = []
        if system_prompt:
            parts.append("System Instructions:\n")
            parts.append(system_prompt)
            parts.append("\n\n")
        if document_context:
            parts.append("Context:\n")
            parts.append(document_context)
            parts.append("\n\n")
        parts.append(user_message)
        parts.append("\n")
        return "".join(parts)
