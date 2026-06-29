# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""
xAI Grok provider shim.
"""

import json
import logging
from .response_normalizers import OpenAIShim
from plugin.framework.url_utils import get_url_path_and_query

log = logging.getLogger(__name__)


class GrokShim(OpenAIShim):
    """Shim for xAI Grok API (OpenAI-compatible)."""

    def build_image_request(self, prompt, model, width, height, steps=None, source_image=None, image_url=None):
        # Aurora is the only xAI image model available via the public API.
        # It does not accept a size parameter yet; omit it.
        endpoint = self.client._endpoint()
        api_path = self.client._api_path()
        url = endpoint + api_path + "/images/generations"

        data = {
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json"
        }
        if model:
            data["model"] = model
        else:
            data["model"] = "aurora"

        if steps:
            data["steps"] = steps

        path = get_url_path_and_query(url)
        return "POST", path, json.dumps(data).encode("utf-8"), self.client._headers()
