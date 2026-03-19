#!/usr/bin/python3
# -*- coding: utf-8 -*-
# basic client for AiHorde
# Authors:
#  * blueturtleai <https://github.com/blueturtleai> Original Code
#  * Igor Támara <https://github.com/ikks>
#
# MIT lICENSE
#
# https://github.com/ikks/aihorde-client/blob/main/LICENSE
# Copied over 2/17/2026
#
# MODIFICATIONS FOR LOCALWRITER 02-22-2026
# - Integrated with project core: uses core.api.sync_request, core.logging for debug/exceptions,
#   and core.constants.USER_AGENT.
# - Removed asyncio/abc dependencies: switched to synchronous patterns suitable for
#   LibreOffice's model which already handles network requests on another thread.
# - Simplified error handling: centralized logic in _raise_horde_http_error for better UX.
# - Removed upstream-specific helper classes: removed HordeClientSettings and
#   ProcedureInformation, as config and logic are handled by WriterAgent's unified systems.
# - Refined informer pattern: used direct callbacks via a simple informer object instead of
#   the upstream abstract interface.


import logging
from datetime import date, datetime

from time import sleep
from typing import Any, Dict, List, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request
from plugin.framework.translation_tool import opustm_hf_translate, OPUSTM_SOURCE_LANGUAGES  # noqa F401

from plugin.modules.http.client import sync_request, format_error_message
from plugin.framework.logging import debug_log, log_exception
from plugin.framework.constants import USER_AGENT

import base64
import gettext
import json
import locale
import math
import socket
import tempfile

_ = gettext.gettext

API_ROOT = "https://aihorde.net/api/v2/"
"""
Base URL for AIHorde API
"""

REGISTER_AI_HORDE_URL = "https://aihorde.net/register"
"""
Url to get an API Key from AI Horde
"""

DISCORD_HELP = "https://discord.com/channels/781145214752129095/1020695869927981086"
"""
Join here if the service is showing errors
"""

ANONYMOUS_KEY = "0000000000"

# check between 8 and 15 seconds
CHECK_WAIT = 8
"""
Minimum wait time to check if the image has been generated in seconds
"""

MAX_TIME_REFRESH = 15
"""
Checking for an image generation will be at most in seconds
"""

DEFAULT_MODEL = "stable_diffusion"
"""
Model that is always present for image generation
"""

MIN_WIDTH = 64
"""
Minimum size for the image width, it's recommended to start from 384, most of the
models are trained from 512px
"""
MAX_WIDTH = 3_072
"""
Maximum size for the image width, most of the models are trained at 512px
"""

MIN_HEIGHT = 64
"""
Minimum size for the image height, it's recommended to start from 384, most of the
models are trained from 512px
"""

MAX_HEIGHT = 3_072
"""
Maximum size for the image width, most of the models are trained at 512px
"""

MIN_PROMPT_LENGTH = 10
"""
We aim to get intention from the user to generate an image, this is the minimum of
characters that we request for the prompt    
"""

MAX_MP = 4_194_304  # 2_048 * 2_048 this is 4MP
"""
At most the user should request an image of 4MP
"""

MODELS = [
    "majicMIX realistic",
    "NatViS",
    "noobEvo",
    "Nova Anime XL",
    "Nova Furry Pony",
    "NTR MIX IL-Noob XL",
    "Pony Diffusion XL",
    "Pony Realism",
    "Prefect Pony",
    "Realistic Vision",
    "SDXL 1.0",
    "Stable Cascade 1.0",
    "stable_diffusion",
]
"""
Initial list of models, new ones are downloaded from AiHorde API
"""

INPAINT_MODELS = [
    "A-Zovya RPG Inpainting",
    "Anything Diffusion Inpainting",
    "Epic Diffusion Inpainting",
    "iCoMix Inpainting",
    "Realistic Vision Inpainting",
    "stable_diffusion_inpainting",
]
"""
Initial list of inpainting models, new ones are downloaded from AiHorde API
"""

MESSAGE_PROCESS_INTERRUPTED = "Process interrupted"
"""
Allows to identify when the client received a cancellation
"""

__HORDE_CLIENT_NAME__ = "AiHordeForGimp"
"""
Default Gimp Client.  Was the first to use this client
"""


class IdentifiedError(Exception):
    """
    Exception for identified problems with an URL

    message: explanation of the error
    url: Resource to understand and fix the problem
    """

    def __init__(self, message: str = "A custom error occurred", url: str = ""):
        self.message: str = message
        self.url: str = url
        super().__init__(self.message)

    def __str__(self):
        return self.message


class AiHordeClient:
    """
    Interaction with AI Horde platform, currently supports:
    * Fetching the most used models in the month
    * Reviewing the credits of an api_key
    * Requesting an image async and go all the way down until getting the image
    * Checking if there is a newer version of the frontend client

    Attributes:

    settings: configured in the constructor and later updated
    """

    # check model updates
    MAX_DAYS_MODEL_UPDATE = 5
    """
    We check at least this number of days for new models
    """

    MAX_MODELS_LIST = 50
    """
    Max Number of models to be presented to the user
    """

    CHECK_WAIT = 5
    """
    Number of seconds to wait before checking again if the image is generated
    """

    MAX_TIME_REFRESH = 15
    """
    If we are in a queue waiting, this is the max time in seconds before asking
    if we are still in queue
    """

    MODEL_REQUIREMENTS_URL = "https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/refs/heads/main/stable_diffusion.json"
    """
    URL of model requirements, the information is injected in the payload to have defaults and avoid warnings
    """

    def __init__(
        self,
        client_version: str,
        url_version_update: str,
        client_help_url: str,
        client_download_url: str,
        settings: json = None,
        client_name: str = __HORDE_CLIENT_NAME__,
        informer=None,
    ):
        """
        Creates an AI Horde client. informer must provide: update_status(text, progress),
        set_finished(), show_error(msg, url=""), set_generated_image_url_status(url, valid_to),
        get_generated_image_url_status(), and optionally get_toolkit() for UI pump during HTTP.
        """
        if informer is None:
            raise IdentifiedError("You must provide an informer")
        if settings is None:
            self.settings = {"api_key": ANONYMOUS_KEY}
        else:
            self.settings: json = settings

        if "max_wait_minutes" not in self.settings:
            self.settings["max_wait_minutes"] = 1

        self.client_version: str = client_version

        # When the async request is succesfull, we store the status_url to download
        # later if there is a problem
        self.status_url: str = ""
        self.wait_time: int = 1000

        self.url_version_update: str = url_version_update
        self.client_help_url: str = client_help_url
        self.client_download_url: str = client_download_url

        self.api_key: str = self.settings.get("aihorde_api_key") or self.settings.get("api_key", ANONYMOUS_KEY)
        self.client_name: str = client_name
        self.headers: json = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "apikey": self.api_key,
            "Client-Agent": self.client_name,
            "User-Agent": USER_AGENT,
        }
        self.informer = informer
        self.progress: float = 0.0
        self.progress_text: str = _("Starting...")
        self.warnings: List[Dict[str, Any]] = []

        # Sync informer and async request
        self.finished_task: bool = True
        self.censored: bool = False
        self.max_time = datetime.now().timestamp() + (
            60 * self.settings["max_wait_minutes"]
        )
        self.factor = 5 / (3.0 * settings["max_wait_minutes"])
        dt = self.headers.copy()
        del dt["apikey"]
        # Beware, not logging the api_key
        debug_log(str(dt), context="AIHorde", level=logging.DEBUG)

        self._should_stop = False
        self.process_interrupted = False
        self.kudos_cost = 0

    def __url_open__(
        self,
        url: Union[str, Request],
        timeout: float = 10,
        refresh_each: float = 0.5,
        only_read=False,
    ) -> None:
        """
        Open a URL (GET or POST).
        Stores the result in self.response_data; on error raises.
        """
        if self._should_stop:
            self.process_interrupted = True
            raise IdentifiedError(MESSAGE_PROCESS_INTERRUPTED)

        self.response_data = None

        # Only send AI Horde headers to AI Horde API endpoints
        actual_headers = None
        if isinstance(url, str) and url.startswith(API_ROOT):
            actual_headers = self.headers
        elif not isinstance(url, str):
            # If it's a Request object, headers are already inside
            pass

        debug_log(f"Requesting URL: {getattr(url, 'full_url', url)}", context="AIHorde", level=logging.DEBUG)
        try:
            if only_read:
                self.response_data = sync_request(
                    url, timeout=timeout, parse_json=False, 
                    headers=actual_headers
                )
            else:
                self.response_data = sync_request(
                    url, timeout=timeout, parse_json=True,
                    headers=actual_headers
                )
        except Exception as ex:
            log_exception(ex, context="AIHorde")
            raise ex

        if self._should_stop:
            self.process_interrupted = True
            raise IdentifiedError(MESSAGE_PROCESS_INTERRUPTED)

    def __update_models_requirements__(self) -> None:
        """
        Downloads model requirements.
        Usually it is a value to be updated, taking the lowest possible value.
        Add range when min and/or max are present as prefix of an attribute,
        the range is stored under the same name of the prefix attribute
        replaced.

        For example min_steps  and max_steps become range_steps
        max_cfg_scale becomes range_cfg_scale.

        Modifies self.settings["local_settings"]["requirements"]
        """
        # download json
        # filter the models that have requirements rules, store
        # the rules processed to be used later easily.
        # Store fixed value and range when possible
        # clip_skip
        # cfg_scale
        #
        # min_steps max_steps
        # min_cfg_scale max_cfg_scale
        # max_cfg_scale can be alone
        # [samplers]   -> can be single
        # [schedulers] -> can be single
        #

        if "local_settings" not in self.settings:
            return

        # Cache requirements for 7 days
        previous_update = self.settings["local_settings"].get("date_requirements_updated", "2025-07-01")
        today = datetime.now().date()
        try:
            days_updated = (today - date(*[int(i) for i in previous_update.split("-")])).days
        except Exception:
            days_updated = 999 

        if days_updated < 7 and "requirements" in self.settings["local_settings"]:
            debug_log(f"No need to update requirements {previous_update}", context="AIHorde", level=logging.DEBUG)
            return

        debug_log("Getting requirements for models", context="AIHorde", level=logging.DEBUG)
        url = self.MODEL_REQUIREMENTS_URL
        self.progress_text = _("Updating model requirements...")
        self.__url_open__(url)
        model_information = self.response_data
        req_info = {}

        for model, reqs in model_information.items():
            if "requirements" not in reqs:
                continue
            req_info[model] = {}
            # Model with requirement
            settings_range = {}
            for name, val in reqs["requirements"].items():
                # extract range where possible
                if name.startswith("max_"):
                    name_req = "range_" + name[4:]
                    if name_req in settings_range:
                        settings_range[name_req][1] = val
                    else:
                        settings_range[name_req] = [0, val]
                elif name.startswith("min_"):
                    name_req = "range_" + name[4:]
                    if name_req in settings_range:
                        settings_range[name_req][0] = val
                    else:
                        settings_range[name_req] = [val, val]
                else:
                    req_info[model][name] = val

            for name, range_vals in settings_range.items():
                if range_vals[0] == range_vals[1]:
                    req_info[model][name[6:]] = range_vals[0]
                else:
                    req_info[model][name] = range_vals

        debug_log(f"We have requirements for {len(req_info)} models", context="AIHorde", level=logging.DEBUG)

        if "requirements" not in self.settings["local_settings"]:
            debug_log("Creating requirements in local_settings", context="AIHorde", level=logging.DEBUG)
            self.settings["local_settings"]["requirements"] = req_info
        else:
            debug_log("Updating requirements in local_settings", context="AIHorde", level=logging.DEBUG)
            self.settings["local_settings"]["requirements"].update(req_info)
        
        self.settings["local_settings"]["date_requirements_updated"] = today.strftime("%Y-%m-%d")

    def __get_model_requirements__(self, model: str) -> json:
        """
        Given the name of a model, fetch the requirements if any,
        to have the opportunity to mix the requirements for the
        model.

        Replaces values that must be fixed and if a value is out
        of range replaces by the min possible value of the range,
        if it was a list of possible values like schedulers, the
        key is replaced by scheduler_name and is enforced to have
        a valid value, if it resulted that was a wrong value,
        takes the first available option.

        Intended to set defaults for the model with the requirements
        present in self.MODEL_REQUIREMENTS_URL json

        The json return has keys with range_ or configuration requirements
        such as steps, cfg_scale, clip_skip, name of a sampler or a scheduler.
        """
        reqs = {}
        if not self.settings or "local_settings" not in self.settings:
            debug_log("Too brand new... ", context="AIHorde", level=logging.DEBUG)
            self.settings["local_settings"] = {}
        if "requirements" not in self.settings["local_settings"]:
            text_doing = self.progress_text
            self.__update_models_requirements__()
            self.progress_text = text_doing

        settings = self.settings["local_settings"]["requirements"].get(model, {})

        if not settings:
            debug_log(f"No requirements for {model}", context="AIHorde", level=logging.DEBUG)
            return reqs

        for key, val in settings.items():
            if key.startswith("range_") and (
                key[6:] not in settings
                or (settings[key[6:]] < val[0])
                or (val[1] < settings[key[6:]])
            ):
                reqs[key[6:]] = val[0]
            elif isinstance(val, list):
                key_name = key[:-1] + "_name"
                if key_name not in settings or settings[key_name] not in val:
                    reqs[key_name] = val[0]
            else:
                reqs[key] = val

        debug_log(f"Requirements for {model} are {reqs}", context="AIHorde", level=logging.DEBUG)
        return reqs

    def __get_model_restrictions__(self, model: str) -> json:
        """
        Returns a json that offers for each key a fixed value or
        a range for the requirements present in self.settings["local_settings"].
         * Fixed Value
         * Range

        Most commonly the result is an empty json.

        Intended for UI validation.

        Can offer range for initial min or max values, and also a
        list of strings or fixed values.
        """
        return self.settings.get("requirements", {model: {}}).get(model, {})

    def cancel_process(self):
        """
        Interrupts the process.  The effect is to finish the process with
        an IdentifiedException with MESSAGE_PROCESS_INTERRUPTED
        """
        self._should_stop = True

    def refresh_models(self):
        """
        Refreshes the model list with the MAX_MODELS_LIST more used including
        always stable_diffusion if not specified, we update self.settings to
        store the date when the models were refreshed.

        Informs if there are new models.
        """
        default_models = MODELS
        self.staging = "Refresh models"
        previous_update = self.settings.get(
            "local_settings", {"date_refreshed_models": "2025-07-01"}
        ).get("date_refreshed_models", "2025-07-01")
        today = datetime.now().date()
        days_updated = (
            today - date(*[int(i) for i in previous_update.split("-")])
        ).days
        if days_updated < AiHordeClient.MAX_DAYS_MODEL_UPDATE:
            debug_log(f"No need to update models {previous_update}", context="AIHorde", level=logging.DEBUG)
            return

        debug_log("time to update models", context="AIHorde", level=logging.DEBUG)
        locals = self.settings.get("local_settings", {"models": MODELS})
        locals["date_refreshed_models"] = today.strftime("%Y-%m-%d")

        url = API_ROOT + "stats/img/models?model_state=known"
        self.headers["X-Fields"] = "month"

        self.progress_text = _("Updating Models...")
        self.__inform_progress__()
        try:
            self.__url_open__(url)
            del self.headers["X-Fields"]
        except (socket.timeout, TimeoutError) as ex:
            debug_log(format_error_message(ex, level=logging.ERROR), context="AIHorde")
            return
        except (HTTPError, URLError) as ex:
            debug_log(format_error_message(ex, level=logging.ERROR), context="AIHorde")
            return

        # Select the most popular models
        popular_models = sorted(
            [(key, val) for key, val in self.response_data["month"].items()],
            key=lambda c: c[1],
            reverse=True,
        )
        debug_log(f"Downloaded {len(popular_models)}", context="AIHorde", level=logging.DEBUG)
        if self.settings.get("mode", "") == "MODE_INPAINTING":
            popular_models = [
                (key, val)
                for key, val in popular_models
                if key.lower().count("inpaint") > 0
            ][: AiHordeClient.MAX_MODELS_LIST]
            default_models = INPAINT_MODELS
        else:
            popular_models = [
                (key, val)
                for key, val in popular_models
                if key.lower().count("inpaint") == 0
            ][: AiHordeClient.MAX_MODELS_LIST]

        fetched_models = [model[0] for model in popular_models]
        default_model = self.settings.get("default_model", DEFAULT_MODEL)
        if default_model not in fetched_models:
            fetched_models.append(default_model)
        if len(fetched_models) > 3:
            compare = set(fetched_models)
            new_models = compare.difference(locals.get("models", default_models))
            if new_models:
                debug_log(f"New models {len(new_models)}", context="AIHorde", level=logging.DEBUG)
                locals["models"] = sorted(fetched_models, key=lambda c: c.upper())

        self.settings["local_settings"] = locals

        self.__update_models_requirements__()
        if "model" in self.settings:
            if self.settings["model"] not in locals["models"]:
                self.settings["model"] = locals["models"][0]
        debug_log(str(self.settings["local_settings"]), context="AIHorde", level=logging.DEBUG)

    def check_update(self) -> str:
        """
        Inform the user regarding a plugin update. Returns "" if the
        installed is the latest one. Else the localized message,
        defaulting to english if there is no locale for the message.
        """
        message = ""

        if getattr(self.informer, "has_asked_for_update", lambda: False)():
            debug_log(
                "We already checked for a new version during this session",
                context="AIHorde",
                level=logging.DEBUG)
            return ""
        debug_log("Checking for update", context="AIHorde", level=logging.DEBUG)

        try:
            # Check for updates by fetching version information from a URL
            url = self.url_version_update
            self.__url_open__(url, 15)
            data = self.response_data

            # During this session we will not check for update again
            getattr(self.informer, "just_asked_for_update", lambda: None)()
            local_version = (*(int(i) for i in str(self.client_version).split(".")),)
            if isinstance(data["version"], int):
                # incoming_version has a deprecated format, local is newer
                return ""
            incoming_version = (*(int(i) for i in data["version"].split(".")),)

            if local_version < incoming_version:
                lang = locale.getlocale()[0][:2]
                message = data["message"].get(lang, data["message"]["en"])
        except (HTTPError, URLError) as ex:
            message = format_error_message(ex)
        return message

    def get_balance(self) -> str:
        """
        Given an AI Horde token, present in the api_key,
        returns the balance for the account. If happens to be an
        anonymous account, invites to register
        """
        if self.api_key == ANONYMOUS_KEY:
            return _("Register at ") + REGISTER_AI_HORDE_URL
        url = API_ROOT + "find_user"
        request = Request(url, headers=self.headers)
        try:
            self.__url_open__(request, 15)
            data = self.response_data
            debug_log(str(data), context="AIHorde", level=logging.DEBUG)
            return _("You have {} kudos").format(data["kudos"])
        except KeyError as ex:
            debug_log(f"find_user endpoint is having problems {ex}", context="AIHorde", level=logging.DEBUG)
            debug_log(f"response was {data}", context="AIHorde", level=logging.DEBUG)
        except HTTPError as ex:
            if ex.code == 404:
                raise IdentifiedError(
                    _(
                        "«{}» is not a valid API KEY, double check it or create a new one"
                    ).format(self.api_key),
                    REGISTER_AI_HORDE_URL,
                )
            elif ex.code == 403:
                raise IdentifiedError(
                    _(
                        "At this moment we can not process your request, please try again later.  If this is happening for a long period of time, please let us know via Discord"
                    ),
                    DISCORD_HELP,
                )
            debug_log("Not able to fetch kudos", context="AIHorde", level=logging.DEBUG)
            raise (ex)
        return _("Problem requesting kudos")

    def _raise_horde_http_error(self, ex: HTTPError, from_status_check: bool = False):
        """
        Map known HTTP errors to IdentifiedError. Used by generate_image inner and outer handlers.
        from_status_check: True when handling errors from __check_if_ready__/__get_images__ (404/403 with status URL).
        """
        if ex.code == 503:
            raise IdentifiedError(
                _(
                    "The Horde is in maintenance mode, please try again later, if you have tried and the service does not respond for hours, please contact via Discord"
                ),
                DISCORD_HELP,
            )
        if not from_status_check:
            if ex.code == 429:
                raise IdentifiedError(
                    _(
                        "You have made too many requests, please wait for them to finish, and try again later"
                    )
                )
            if ex.code == 401:
                raise IdentifiedError(
                    _(
                        "Seems that «{}» has problems, double check it, create a new one or join Discord to ask for help"
                    ).format(self.api_key),
                    DISCORD_HELP,
                )
        if ex.code in (403, 404) and from_status_check:
            result = self.informer.get_generated_image_url_status()
            if result:
                raise IdentifiedError(
                    _("We hit an error, still: ") + result[2], result[0]
                )
        if ex.code == 403:
            raise IdentifiedError(
                _(
                    "At this moment we can not process your request, please try again later.  If this is happening for a long period of time, please let us know via Discord"
                ),
                DISCORD_HELP,
            )
        if ex.code == 404 and from_status_check:
            raise IdentifiedError(
                _("No longer valid, please try again.  Your request took too long")
            )

    def generate_image(self, options: json) -> [str]:
        """
        options have been prefilled for the selected model
        informer will be acknowledged on the process via show_progress
        Executes the flow to get an image from AI Horde

        1. Invokes endpoint to launch a work for image generation
        2. Reviews the status of the work
        3. Waits until the max_wait_minutes for the generation of
        the image passes or the image is generated
        4. Retrieves the resulting images and returns the local path of
        the downloaded images

        When no success, returns [].  raises exceptions, but tries to
        offer helpful messages.

        Also checks for update of the plugin.

        Downloads the most popular models and reviews the requirements
        to adjust the configuration to avoid warnings of misconfigurations
        outside the requirements.
        """
        images_names = []
        self.status_url = ""
        self.wait_time: int = 1000
        self.stage = "Nothing"
        self.settings.update(options)
        self.api_key = options["api_key"]
        self.headers["apikey"] = self.api_key
        self.check_counter = 1
        self.check_max = (options["max_wait_minutes"] * 60) / AiHordeClient.CHECK_WAIT
        # Id assigned when requesting the generation of an image
        self.id = ""

        # Used for the progressbar.  We depend on the max time the user indicated
        self.max_time = datetime.now().timestamp() + options["max_wait_minutes"] * 60
        self.factor = 5 / (
            3.0 * options["max_wait_minutes"]
        )  # Percentage and minutes 100*ellapsed/(max_wait*60)

        self.progress_text = _("Contacting the Horde...")
        try:
            params = {
                "cfg_scale": float(options["prompt_strength"]),
                "steps": int(options["steps"]),
                "seed": options["seed"],
            }
            restrictions = self.__get_model_requirements__(options["model"])
            params.update(restrictions)

            width = max(options["image_width"], MIN_WIDTH)
            width = min(options["image_width"], MAX_WIDTH)
            height = max(options["image_height"], MIN_HEIGHT)
            height = min(options["image_height"], MAX_HEIGHT)

            if width * height > MAX_MP:
                factor = (width * 1.0) / height
                ratio = math.sqrt(MAX_MP / (width * height))
                if factor < 1.0:
                    width = width * ratio * factor
                    height = height * ratio * factor
                else:
                    height = height * ratio / factor
                    width = width * ratio / factor
                width = int(width)
                height = int(height)

            if width % 64 != 0:
                width = int(width / 64) * 64

            if height % 64 != 0:
                height = int(height / 64) * 64

            params.update({"width": int(width)})
            params.update({"height": int(height)})

            data_to_send = {
                "params": params,
                "prompt": options["prompt"],
                "nsfw": options["nsfw"],
                "censor_nsfw": options["censor_nsfw"],
                "r2": True,
            }

            data_to_send.update({"models": [options["model"]]})

            mode = options.get("mode", "")
            if mode == "MODE_IMG2IMG":
                data_to_send.update({"source_image": options["source_image"]})
                data_to_send.update({"source_processing": "img2img"})
                data_to_send["params"].update(
                    {"denoising_strength": (1 - float(options["init_strength"]))}
                )
                data_to_send["params"].update({"n": options["nimages"]})
            elif mode == "MODE_INPAINTING":
                data_to_send.update({"source_image": options["source_image"]})
                data_to_send.update({"source_processing": "inpainting"})
                data_to_send["params"].update({"n": options["nimages"]})

            dt = data_to_send.copy()
            if "source_image" in dt:
                del dt["source_image"]
                dt["source_image_size"] = len(data_to_send["source_image"])
            debug_log(str(dt), context="AIHorde", level=logging.DEBUG)

            post_data = json.dumps(data_to_send).encode("utf-8")

            url = f"{API_ROOT}generate/async"

            request = Request(url, headers=self.headers, data=post_data)
            try:
                self.stage = "Contacting..."
                self.__inform_progress__()
                self.__url_open__(request, 15)
                debug_log("Initial request completed, processing response...", context="AIHorde", level=logging.INFO)
                data = self.response_data
                debug_log(str(data), context="AIHorde", level=logging.DEBUG)
                if "warnings" in data:
                    self.warnings = data["warnings"]
                text = _("Horde Contacted")
                self.kudos_cost = data["kudos"]
                self.settings["kudos_cost"] = self.kudos_cost
                debug_log(text + f" {self.check_counter} {self.progress}", context="AIHorde", level=logging.DEBUG)
                self.progress_text = text
                self.__inform_progress__()
                self.id = data["id"]
                self.status_url = f"{API_ROOT}generate/status/{self.id}"
                self.informer.set_generated_image_url_status(self.status_url, 600)
                try:
                    status_info = self.informer.get_generated_image_url_status()
                    if status_info and len(status_info) > 2:
                        debug_log(status_info[2], context="AIHorde", level=logging.DEBUG)
                except Exception:
                    pass
                self.wait_time = data.get("wait_time", self.wait_time)
            except (socket.timeout, TimeoutError) as ex:
                message = _(
                    "When trying to ask for the image, the Horde was too slow, try again later"
                )
                log_exception(ex, context="AIHorde")
                raise IdentifiedError(message)
            except HTTPError as ex:
                self._raise_horde_http_error(ex)
                data = None
                try:
                    raw = ex.read().decode("utf-8")
                    data = json.loads(raw)
                    message = data.get("message", format_error_message(ex))
                    if data.get("rc", "") == "KudosUpfront":
                        if self.api_key == ANONYMOUS_KEY:
                            message = (
                                _(
                                    f"Register at {REGISTER_AI_HORDE_URL} and use your key to improve your rate success. Detail:"
                                )
                                + f" {message}."
                            )
                        else:
                            message = (
                                f"{self.client_help_url} "
                                + _("to learn to earn kudos. Detail:")
                                + f" {message}."
                            )
                except Exception as ex2:
                    log_exception(ex2, context="AIHorde")
                    message = format_error_message(ex)
                debug_log("%s %s" % (message, data), context="AIHorde", level=logging.DEBUG)
                if self.api_key == ANONYMOUS_KEY and REGISTER_AI_HORDE_URL in message:
                    self.informer.show_error(f"{message}", url=REGISTER_AI_HORDE_URL)
                else:
                    self.informer.show_error(f"{message}")
                return ""
            except URLError as ex:
                log_exception(ex, context="AIHorde")
                self.informer.show_error(format_error_message(ex))
                return ""
            except Exception as ex:
                log_exception(ex, context="AIHorde")
                url = ""
                if isinstance(ex, IdentifiedError):
                    url = ex.url
                self.informer.show_error(
                    _("Service failed: ") + format_error_message(ex), url=url
                )
                return ""

            self.__check_if_ready__()
            images = self.__get_images__()
            images_names = self.__get_images_filenames__(images)

        except IdentifiedError as ex:
            if ex.url:
                self.informer.show_error(str(ex), url=ex.url)
            else:
                self.informer.show_error(str(ex))
            return ""
        except HTTPError as ex:
            self._raise_horde_http_error(ex, from_status_check=True)
        except Exception as ex:
            log_exception(ex, context="AIHorde")
            self.informer.show_error(
                _("Service failed: ") + format_error_message(ex)
            )
            return ""
        finally:
            self.informer.set_finished()

        return images_names

    def __inform_progress__(self):
        """
        Reports to informer the progress updating the attribute progress
        with the percentage elapsed time since the job started
        """
        progress = 100 - (int(self.max_time - datetime.now().timestamp()) * self.factor)

        debug_log(
            f"[{progress:.2f}/{self.settings['max_wait_minutes'] * 60}] {self.progress_text}",
            context="AIHorde",
            level=logging.DEBUG)

        if self.informer and (progress != self.progress or self.progress_text != getattr(self, '_last_progress_text', None)):
            self.informer.update_status(self.progress_text, progress)
            self.progress = progress
            self._last_progress_text = self.progress_text

    def __check_if_ready__(self) -> bool:
        """
        Queries AI horde API to check if the requested image has been generated,
        returns False if is not ready, otherwise True.
        When the time to get an image has been reached raises an Exception, also
        throws exceptions when there are network problems.

        Calls itself until max_time has been reached or the information from the API
        helps to conclude that the time will be longer than user configured.

        self.id holds the ID of the task that generates the image
        * Uses self.response_data
        * Uses self.check_counter
        * Uses self.max_time
        * Queries self.api_key

        Raises and propagates exceptions
        """
        debug_log(f"Checking status for job ID: {self.id}", context="AIHorde", level=logging.DEBUG)
        url = f"{API_ROOT}generate/check/{self.id}"

        self.__url_open__(url)
        data = self.response_data

        debug_log(str(data), context="AIHorde", level=logging.DEBUG)

        self.check_counter = self.check_counter + 1

        if data["finished"]:
            self.progress_text = _("Downloading generated image...")
            self.__inform_progress__()
            return True

        if data["processing"] == 0:
            if data["queue_position"] == 0:
                text = _("You are first in the queue")
            else:
                text = _("Queue position: ") + str(data["queue_position"])
            debug_log(f"{text} (wait_time: {data.get('wait_time')})", context="AIHorde", level=logging.DEBUG)
        elif data["processing"] > 0:
            text = _("Generating...")
            debug_log(f"{text} (counter: {self.check_counter}, progress: {self.progress}, level=logging.DEBUG)", context="AIHorde")
        self.progress_text = text

        if self.check_counter < self.check_max:
            if (
                data["processing"] == 0
                and data["wait_time"] + datetime.now().timestamp() > self.max_time
            ):
                # If we are in queue, we will not be served in time
                debug_log(str(data), context="AIHorde", level=logging.DEBUG)
                self.informer.set_generated_image_url_status(
                    self.status_url, data["wait_time"]
                )
                debug_log(self.informer.get_generated_image_url_status()[2], context="AIHorde", level=logging.DEBUG)
                if self.api_key == ANONYMOUS_KEY:
                    message = (
                        _("Get a free API Key at ")
                        + REGISTER_AI_HORDE_URL
                        + _(
                            ".\n This model takes more time than your current configuration."
                        )
                    )
                    raise IdentifiedError(message, url=REGISTER_AI_HORDE_URL)
                else:
                    message = (
                        _("Please try another model,")
                        + _("{} would take more time than you configured,").format(
                            self.settings["model"]
                        )
                        + _(" or try again later.")
                    )
                    raise IdentifiedError(message, url=self.status_url)

            if data["is_possible"] is True:
                # We still have time to wait, given that the status is processing, we
                # wait between 5 secs and 15 secs to check again
                wait_time = min(
                    max(AiHordeClient.CHECK_WAIT, int(data["wait_time"] / 2)),
                    AiHordeClient.MAX_TIME_REFRESH,
                )
                for i in range(1, wait_time * 2):
                    sleep(0.5)
                    self.__inform_progress__()
                return self.__check_if_ready__()
            else:
                debug_log(str(data), context="AIHorde", level=logging.DEBUG)
                raise IdentifiedError(
                    _(
                        "There are no workers available with these settings. Please try again later."
                    )
                )
        else:
            if self.api_key == ANONYMOUS_KEY:
                message = (
                    _("Get an Api key for free at ")
                    + REGISTER_AI_HORDE_URL
                    + _(
                        ".\n This model takes more time than your current configuration."
                    )
                )
                raise IdentifiedError(message, url=REGISTER_AI_HORDE_URL)
            else:
                minutes = (self.check_max * AiHordeClient.CHECK_WAIT) / 60
                debug_log(str(data), context="AIHorde", level=logging.DEBUG)
                if minutes == 1:
                    raise IdentifiedError(
                        _("Probably your image will take one additional minute.")
                        + " "
                        + _("Please try again later.")
                    )
                else:
                    raise IdentifiedError(
                        _(
                            "Probably your image will take {} additional minutes."
                        ).format(minutes)
                        + _("Please try again later.")
                    )
        return False

    def __get_images__(self):
        """
        Returns the image information of a generated image.
        At this stage AI horde has generated the images and it's time
        to download them all.
        """
        self.stage = "Getting images"
        url = f"{API_ROOT}generate/status/{self.id}"
        self.progress_text = _("Fetching images...")
        self.__inform_progress__()
        self.__url_open__(url)
        data = self.response_data
        debug_log(str(data), context="AIHorde", level=logging.DEBUG)
        if len(data["generations"]) == 0:
            return []
        if data["generations"][0]["censored"]:
            image = data["generations"][0]
            message = f"«{self.settings['prompt']}»" + _(
                " is censored, try changing the prompt wording"
            )
            debug_log(message, context="AIHorde", level=logging.DEBUG)
            debug_log(str(image["gen_metadata"], level=logging.DEBUG), context="AIHorde")
            self.informer.show_error(message, title="warning")
            self.censored = True

        return data["generations"]

    def __get_images_filenames__(self, images: List[Dict[str, Any]]) -> List[str]:
        """
        Downloads the generated images and returns the full path of the
        downloaded images.
        """
        self.stage = "Downloading images"
        debug_log("Start to download generated images", context="AIHorde", level=logging.INFO)
        generated_filenames = []
        cont = 1
        nimages = len(images)
        for image in images:
            with tempfile.NamedTemporaryFile(
                "wb+", delete=False, suffix=".webp"
            ) as generated_file:
                if self.settings.get("seed", "") == "":
                    self.settings["seed"] = image["seed"]
                if image["img"].startswith("https"):
                    debug_log(f"Downloading {image['img']}", context="AIHorde", level=logging.DEBUG)
                    if nimages == 1:
                        self.progress_text = _("Downloading result...")
                    else:
                        self.progress_text = (
                            _("Downloading image") + f" {cont}/{nimages}"
                        )
                    self.__inform_progress__()
                    self.__url_open__(image["img"], only_read=True)
                    bytes = self.response_data
                else:
                    debug_log(f"Storing embebed image {cont}", context="AIHorde", level=logging.DEBUG)
                    bytes = base64.b64decode(image["img"])

                debug_log(f"Dumping to {generated_file.name}", context="AIHorde", level=logging.DEBUG)
                generated_file.write(bytes)
                generated_filenames.append(generated_file.name)
                cont += 1
        if self.warnings:
            message = (
                _(
                    "You may need to reduce your settings or choose another model, or you may have been censored. Horde message"
                )
                + ":\n * "
                + "\n * ".join([i["message"] for i in self.warnings])
            )
            debug_log(str(self.warnings, level=logging.WARNING), context="AIHorde")
            self.informer.show_error(message, title="warning")
            self.warnings = []
        self.refresh_models()
        return generated_filenames

    def get_imagename(self) -> str:
        """
        Returns a name and the model for the image, intended to be used as identifier
        To be run after a succesful generation
        """
        if "prompt" not in self.settings:
            return "AIHorde will be invoked and this image will appear"
        return self.settings["prompt"] + " " + self.settings["model"]

    def get_title(self) -> str:
        """
        Returns the prompt and model used and attribution to AIHorde
        Intended to be used as the title to offer the user some information
        """
        if "prompt" not in self.settings:
            return "AIHorde will be invoked and this image will appear"
        return self.settings["prompt"] + _(" generated by ") + "AIHorde"

    def get_tooltip(self) -> str:
        """
        Intended for assistive technologies, returns prompt and model used
        """
        if "prompt" not in self.settings:
            return "AIHorde will be invoked and this image will appear"
        return (
            self.settings["prompt"]
            + _(" with ")
            + self.settings["model"]
            + _(" generated by ")
            + "AIHorde"
        )

    def get_full_description(self) -> str:
        """
        Returns the options used for image_generation
        Useful for reproducibility. Intended to be run after a succesful generation
        """
        if "prompt" not in self.settings:
            return "AIhorde shall be working sometime in the future"

        options = [
            "prompt",
            "model",
            "seed",
            "image_width",
            "image_height",
            "prompt_strength",
            "steps",
            "nsfw",
            "censor_nsfw",
            "kudos_cost",
        ]

        result = ["".join((op, " : ", str(self.settings[op]))) for op in options]

        return "\n".join(result)

    def get_settings(self) -> json:
        """
        Returns the stored settings
        """
        return self.settings

    def set_settings(self, settings: json):
        """
        Store the given settings, useful when fetching from a file or updating
        based on user selection.
        """
        self.settings = settings


# * [ ] Add support for styles
# * [ ] Fetch list of styles https://aihorde.net/api/v2/styles/image?sort=popular&page=1
# * [ ] Create an image based on a style. Makes a post wit the information from the style
# * [ ] Fetch my styles
# * [ ] Have a default list of styles
# * [ ] Create a style POST with a tag identifying the user to get own styles filtering by tag
# * [ ] Fetch information for a particular style
# * [ ] Delete a style
# * [ ] Modify a style
# * [ ] Clone style
# * [ ] Upload an example style
