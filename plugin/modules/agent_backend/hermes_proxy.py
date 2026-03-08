"""Hermes agent backend adapter. Keeps one long-lived Hermes CLI process; each Send writes to
stdin and streams the next response. Conversation context is preserved across messages.

We never close stdin (or the PTY write side)—closing it would signal EOF and cause Hermes to exit.
Only stop() terminates the process; between messages the process stays running with stdin open.

Expects Hermes to be configured with WriterAgent's MCP server in ~/.hermes/config.yaml.
"""

import re
from plugin.modules.agent_backend.cli_backend import CLIProcessBackend, strip_ansi

_HERMES_PROMPT_CHAR = "\u276f"


class HermesBackend(CLIProcessBackend):
    backend_id = "hermes"
    display_name = "Hermes"

    def get_default_cmd(self):
        return "hermes"

    def is_ready_prompt(self, line):
        if not line or _HERMES_PROMPT_CHAR not in line:
            return False
        s = strip_ansi(line).strip()
        s = re.sub(r"[\s\u2500-\u257f\-]+", "", s)
        return s == _HERMES_PROMPT_CHAR

    def is_end_of_response(self, line):
        # Hermes usually ends its response with the prompt char again, or "Goodbye"
        return self.is_ready_prompt(line) or "Goodbye" in line

    def format_input(self, user_message, document_context, document_url, system_prompt, selection_text, **kwargs):
        parts = []
        if document_context:
            parts.append("Document context:\n\n")
            parts.append(document_context)
            parts.append("\n\n")
        if system_prompt:
            parts.append("Instructions:\n\n")
            parts.append(system_prompt)
            parts.append("\n\n")
        parts.append("User: ")
        parts.append(user_message)
        parts.append("\n")
        return "".join(parts)
