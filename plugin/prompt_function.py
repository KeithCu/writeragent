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

# Enable debug logging
DEBUG = True

def debug_log(message):
    """Debug logging function"""
    if DEBUG:
        try:
            # Try to write to a debug file
            debug_file = os.path.expanduser("~/libreoffice_prompt_debug.log")
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(f"{message}\n")
        except Exception:
            # Fallback to stdout
            print(f"DEBUG: {message}")
            sys.stdout.flush()

class PromptFunction(unohelper.Base, XPromptFunction):
    def __init__(self, ctx):
        debug_log("=== PromptFunction.__init__ called ===")
        self.ctx = ctx
        self.client = None

    def prompt(self, message, systemPrompt, model, maxTokens):
        debug_log(f"=== PromptFunction.PROMPT({message}) called ===")
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
                debug_log("PROMPT error: %s" % str(e))
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
