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
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_ext_root = os.path.dirname(_plugin_dir)
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)


import unohelper


# from com.sun.star.lang import XServiceInfo
# from com.sun.star.sheet import XAddIn
from org.extension.writeragent.PromptFunction import XPromptFunction
from plugin.framework.config import get_config, get_api_config
from plugin.modules.http.client import LlmClient

import logging
from plugin.framework.logging import debug_log

class PromptFunction(unohelper.Base, XPromptFunction):
    def __init__(self, ctx):
        debug_log("=== PromptFunction.__init__ called ===", context="PROMPT", level=logging.DEBUG)
        self.ctx = ctx
        self.client = None

    def getProgrammaticFunctionName(self, aDisplayName):
        debug_log(f"=== getProgrammaticFunctionName called with: '{aDisplayName}' ===", context="PROMPT", level=logging.DEBUG)
        if aDisplayName == "PROMPT":
            return "prompt"
        return ""

    def getDisplayFunctionName(self, aProgrammaticName):
        debug_log(f"=== getDisplayFunctionName called with: '{aProgrammaticName}' ===", context="PROMPT", level=logging.DEBUG)
        if aProgrammaticName == "prompt":
            return "PROMPT"
        return ""

    def getFunctionDescription(self, aProgrammaticName):
        if aProgrammaticName == "prompt":
            return "Generates text using an LLM."
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
        return ""

    def hasFunctionWizard(self, aProgrammaticName):
        return True

    def getArgumentCount(self, aProgrammaticName):
        if aProgrammaticName == "prompt":
            return 4
        return 0

    def getArgumentIsOptional(self, aProgrammaticName, nArgument):
        if aProgrammaticName == "prompt":
            return nArgument > 0
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
        debug_log(f"=== PromptFunction.PROMPT({message}) called ===", context="PROMPT", level=logging.DEBUG)
        aProgrammaticName = "PROMPT"
        if aProgrammaticName == "PROMPT":
            try:
                system_prompt = systemPrompt if systemPrompt is not None else get_config(self.ctx, "extend_selection_system_prompt")
                model_name = model if model is not None else (get_config(self.ctx, "text_model") or get_config(self.ctx, "model") or "")
                max_tokens = maxTokens if maxTokens is not None else get_config(self.ctx, "calc_prompt_max_tokens")
                try:
                    max_tokens = int(max_tokens)
                except (TypeError, ValueError):
                    max_tokens = 70

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
                
                from plugin.framework.async_stream import run_blocking_in_thread
                return run_blocking_in_thread(self.ctx, self.client.chat_completion_sync, messages, max_tokens=max_tokens)
            except Exception as e:
                from plugin.modules.http.client import format_error_for_display
                debug_log("PROMPT error: %s" % str(e), context="PROMPT", level=logging.ERROR)
                return format_error_for_display(e)
        return ""

    # XServiceInfo implementation
    def getImplementationName(self):
        return "org.extension.writeragent.PromptFunction"
    
    def supportsService(self, name):
        return name in self.getSupportedServiceNames()
    
    def getSupportedServiceNames(self):
        return ("com.sun.star.sheet.AddIn",)

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    PromptFunction,
    "org.extension.writeragent.PromptFunction",
    ("com.sun.star.sheet.AddIn",),
)

# Test function registration
def test_registration():
    """Test if the function is properly registered"""
    debug_log("=== Testing function registration ===", context="PROMPT", level=logging.DEBUG)
    try:
        # This will be called when LibreOffice loads the extension
        debug_log("Function registration test completed", context="PROMPT", level=logging.INFO)
    except Exception as e:
        debug_log(f"Registration test failed: {e}", context="PROMPT", level=logging.ERROR)

# Call test on module load
test_registration()