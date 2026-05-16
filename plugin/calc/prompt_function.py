# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import os
import sys

# Ensure the extension's install directory is on sys.path
# so that "plugin.xxx" imports work correctly.
# We must do this before any imports from "plugin.xyz"
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_ext_root = os.path.dirname(os.path.dirname(_plugin_dir))
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
if os.path.dirname(_plugin_dir) not in sys.path:
    sys.path.insert(0, os.path.dirname(_plugin_dir))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)


import unohelper


# from com.sun.star.lang import XServiceInfo
# from com.sun.star.sheet import XAddIn
try:
    from org.extension.writeragent.PromptFunction import (  # type: ignore
        XPromptFunction as _XPromptFunctionBase,
    )
except ImportError:

    class _XPromptFunctionStub(unohelper.Base):
        pass

    _XPromptFunctionBase = _XPromptFunctionStub

from plugin.framework.config import get_config, get_api_config, get_config_int
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.async_stream import run_blocking_in_thread
from plugin.framework.client.errors import format_error_for_display
from plugin.calc.calc_addin_data import calc_addin_data_to_python, check_python_data_size
from plugin.scripting.run_venv_code import run_code_in_user_venv

import logging

log = logging.getLogger(__name__)


class PromptFunction(unohelper.Base, _XPromptFunctionBase):  # pyright: ignore[reportGeneralTypeIssues] — runtime IDL base from LO  # pyrefly: ignore[invalid-inheritance]
    def __init__(self, ctx):
        log.debug("=== PromptFunction.__init__ called ===")
        self.ctx = ctx
        self.client = None

    def getProgrammaticFunctionName(self, aDisplayName):
        log.debug(f"=== getProgrammaticFunctionName called with: '{aDisplayName}' ===")
        if aDisplayName == "PROMPT":
            return "prompt"
        if aDisplayName == "PYTHON":
            return "python"
        return ""

    def getDisplayFunctionName(self, aProgrammaticName):
        log.debug(f"=== getDisplayFunctionName called with: '{aProgrammaticName}' ===")
        if aProgrammaticName == "prompt":
            return "PROMPT"
        if aProgrammaticName == "python":
            return "PYTHON"
        return ""

    def getFunctionDescription(self, aProgrammaticName):
        if aProgrammaticName == "prompt":
            return "Generates text using an LLM."
        if aProgrammaticName == "python":
            return "Executes Python code in the configured venv and returns the result."
        return ""

    def getArgumentDescription(self, aProgrammaticName, nArgument):
        if aProgrammaticName == "prompt":
            if nArgument == 0:
                return "The prompt to send to the LLM."
            elif nArgument == 1:
                return "The system prompt to use."
            elif nArgument == 2:
                return "The model to use."
            elif nArgument == 3:
                return "The maximum number of tokens to generate."
        if aProgrammaticName == "python":
            if nArgument == 0:
                return "The Python code to execute. Assign output to 'result'."
            elif nArgument == 1:
                return "Optional cell range; values are available as the variable data in the script."
        return ""

    def getArgumentName(self, aProgrammaticName, nArgument):
        if aProgrammaticName == "prompt":
            if nArgument == 0:
                return "message"
            elif nArgument == 1:
                return "system_prompt"
            elif nArgument == 2:
                return "model"
            elif nArgument == 3:
                return "max_tokens"
        if aProgrammaticName == "python":
            if nArgument == 0:
                return "code"
            elif nArgument == 1:
                return "data"
        return ""

    def hasFunctionWizard(self, aProgrammaticName):
        return True

    def getArgumentCount(self, aProgrammaticName):
        if aProgrammaticName == "prompt":
            return 4
        if aProgrammaticName == "python":
            return 2
        return 0

    def getArgumentIsOptional(self, aProgrammaticName, nArgument):
        if aProgrammaticName == "prompt":
            return nArgument > 0
        if aProgrammaticName == "python":
            return nArgument == 1
        return False

    def getProgrammaticCategoryName(self, aProgrammaticName):
        return "Add-In"

    def getDisplayCategoryName(self, aProgrammaticName):
        return "Add-In"

    def getLocale(self):
        return self.ctx.ServiceManager.createInstance("com.sun.star.lang.Locale", ("en", "US", ""))

    def setLocale(self, locale):
        pass

    def load(self, xSomething):
        pass

    def unload(self):
        pass

    def prompt(self, message, systemPrompt, model, maxTokens):
        log.debug(f"=== PromptFunction.PROMPT({message}) called ===")
        try:
            system_prompt = systemPrompt if systemPrompt is not None else get_config(self.ctx, "extend_selection_system_prompt")
            model_name = model if model is not None else (get_config(self.ctx, "text_model") or get_config(self.ctx, "model") or "")
            if maxTokens is not None:
                try:
                    max_tokens = int(maxTokens)
                except (TypeError, ValueError):
                    max_tokens = 70
            else:
                max_tokens = get_config_int(self.ctx, "calc_prompt_max_tokens")

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": message})

            config = get_api_config(self.ctx)
            if model is not None:
                config = dict(config, model=str(model_name))

            if not self.client:
                self.client = LlmClient(config, self.ctx)
            else:
                self.client.config = config

            return run_blocking_in_thread(self.ctx, self.client.chat_completion_sync, messages, max_tokens=max_tokens)
        except Exception as e:
            log.error("PROMPT error: %s" % str(e))
            return format_error_for_display(e)

    def python(self, code, data=None):
        log.debug("=== PromptFunction.PYTHON(%r, data=%r) called ===", code, data)
        try:
            py_data = calc_addin_data_to_python(data)
            if py_data is not None:
                size_err = check_python_data_size(py_data)
                if size_err:
                    return f"Error: {size_err}"
            res = run_blocking_in_thread(self.ctx, run_code_in_user_venv, self.ctx, code, data=py_data)
            if res.get("status") == "ok":
                return res.get("result")
            else:
                return f"Error: {res.get('message') or res.get('error')}"
        except Exception as e:
            log.error("PYTHON error: %s" % str(e))
            return format_error_for_display(e)

    # XServiceInfo implementation
    def getImplementationName(self):
        return "org.extension.writeragent.PromptFunction"

    def supportsService(self, name):
        return name in self.getSupportedServiceNames()

    def getSupportedServiceNames(self):
        return ("com.sun.star.sheet.AddIn",)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PromptFunction, "org.extension.writeragent.PromptFunction", ("com.sun.star.sheet.AddIn",))
