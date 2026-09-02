#!/usr/bin/env python
# coding=utf-8

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Vendored smolagents package.

Import submodules directly (``plugin.contrib.smolagents.utils``, ``.tools``,
…). This file used to star-import every submodule, which meant
``from plugin.contrib.smolagents.utils import BASE_BUILTIN_MODULES`` (used by
compute workers via sandbox.py) also ran ``tools.py`` → ``huggingface_hub``.
That printed to stdout under full pytest:

    Error importing huggingface_hub.hf_api: No module named 'envwrap'
    (or 'envwrap.envwrap' is unknown)

and broke pickle-stdio spawn. Keep this init side-effect free.
"""
__version__ = "1.25.0.dev0"
