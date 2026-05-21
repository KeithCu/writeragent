# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO Calc add-in for =PROMPT() only (LLM path isolated from =PYTHON())."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc._addin_path import ensure_addin_paths

ensure_addin_paths()

import unohelper  # noqa: E402

from plugin.calc.addin_common import CalcFunctionSpec, SingleFunctionAddInBase  # noqa: E402
from plugin.calc.calc_prompt_handlers import execute_prompt_addin  # noqa: E402
from plugin.framework.client.llm_client import LlmClient  # noqa: E402

log = logging.getLogger(__name__)

_PROMPT_SPEC = CalcFunctionSpec(
    display_name="PROMPT",
    programmatic_name="prompt",
    description="Generates text using an LLM.",
    arg_names=("message", "system_prompt", "model", "max_tokens"),
    arg_descriptions=(
        "The prompt to send to the LLM.",
        "The system prompt to use.",
        "The model to use.",
        "The maximum number of tokens to generate.",
    ),
    optional_from=1,
)

try:
    from org.extension.writeragent.PromptFunction import (  # type: ignore
        XPromptFunction as _XPromptFunctionBase,
    )
except ImportError:

    class _XPromptFunctionStub(unohelper.Base):
        pass

    _XPromptFunctionBase = _XPromptFunctionStub


class PromptFunction(SingleFunctionAddInBase, _XPromptFunctionBase):  # pyright: ignore[reportGeneralTypeIssues]  # pyrefly: ignore[invalid-inheritance]
    """Calc add-in: org.extension.writeragent.PromptFunction (=PROMPT)."""

    def __init__(self, ctx: Any) -> None:
        log.debug("=== PromptFunction.__init__ ===")
        super().__init__(ctx, _PROMPT_SPEC)
        self._llm_client: LlmClient | None = None

    def prompt(self, message: str, systemPrompt: Any, model: Any, maxTokens: Any) -> str:
        holder: list[LlmClient | None] = [self._llm_client]
        result = execute_prompt_addin(
            self.ctx,
            message,
            systemPrompt,
            model,
            maxTokens,
            client_holder=holder,
        )
        self._llm_client = holder[0]
        return result

    def getImplementationName(self) -> str:
        return "org.extension.writeragent.PromptFunction"


# Back-compat alias from the split refactor.
PromptAddIn = PromptFunction

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    PromptFunction,
    "org.extension.writeragent.PromptFunction",
    ("com.sun.star.sheet.AddIn",),
)
