#!/usr/bin/python3
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
# -*- coding: utf-8 -*-
# Translation from OpusTM hosted at HF
# Authors:
#  * Igor Támara <https://github.com/ikks>
#
# MIT lICENSE
#
# https://github.com/ikks/aihorde-client/blob/main/LICENSE

import json
from plugin.modules.http.requests import sync_request

API_TRANSLATE_GRADIO = "https://igortamara-opus-translate.hf.space/call/translate"

OPUSTM_SOURCE_LANGUAGES = {
    "af": ("Afrikaans", "Afrikaans"),
    "sq": ("Albanian", "shqip"),
    "ar": ("Arabic", "العربية"),
    "hy": ("Armenian", "Հայերեն"),
    "eu": ("Basque", "euskera"),
    "bn": ("Bengali", "বাংলা"),
    "bg": ("Bulgarian", "български"),
    "ca": ("Catalan", "català"),
    "cs": ("Czech", "čeština"),
    "da": ("Danish", "dansk"),
    "nl": ("Dutch", "Nederlands"),
    "eo": ("Esperanto", "Esperanto"),
    "et": ("Estonian", "eesti keel"),
    "fi": ("Finnish", "suomi"),
    "fr": ("French", "français"),
    "gl": ("Galician", "Galego"),
    "ka": ("Georgian", "ქართული"),
    "de": ("German", "Deutsch"),
    "hi": ("Hindi", "हिन्दी"),
    "hu": ("Hungarian", "magyar"),
    "is": ("Icelandic", "Íslenska"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "ga": ("Irish", "Gaeilge"),
    "it": ("Italian", "italiano"),
    "ja": ("Japanese", "日本語"),
    "kab": ("Kabyle", "Taqbaylit"),
    "rw": ("Kinyarwanda", "kinyaRwanda"),
    "ko": ("Korean", "한국어 [韓國語]"),
    "lv": ("Latvian", "latviešu"),
    "mk": ("Macedonian", "македонски"),
    "ml": ("Malayalam", "മലയാളം"),
    "mr": ("Marathi", "मराठी"),
    "nso": ("Northern Sotho", "Sesotho sa Leboa"),
    "om": ("Oromo", "Afaan Oromo"),
    "pl": ("Polish", "polski"),
    "ru": ("Russian", "Русский"),
    "sk": ("Slovak", "slovenčina"),
    "st": ("Southern Sotho", "Sesotho"),
    "es": ("Spanish", "español"),
    "ss": ("Swati", "siSwati"),
    "sv": ("Swedish", "Svenska"),
    "tl": ("Tagalog", "Tagalog"),
    "th": ("Thai", "ภาษาไทย"),
    "ts": ("Tsonga", "xiTshonga"),
    "tn": ("Tswana", "seTswana"),
    "tr": ("Turkish", "Türkçe"),
    "uk": ("Ukrainian", "Українська"),
    "ve": ("Venda", "tshiVenḓa"),
    "vi": ("Vietnamese", "tiếng việt"),
    "cy": ("Welsh", "Welsh/Cymraeg"),
    "xh": ("Xhosa", "isiXhosa"),
}


# Extracted from https://www.libreoffice.org/download/download-libreoffice/?lang=pick
# 2025-09-04
original_lo_list = [
    ("af", "Afrikaans", "Afrikaans"),
    ("sq", "Albanian", "shqip"),
    ("am", "Amharic", "አማርኛ"),
    ("ar", "Arabic", "العربية"),
    ("hy", "Armenian", "Հայերեն"),
    ("as", "Assamese", "অসমীয়া"),
    ("ast", "Asturian", "Asturianu"),
    ("eu", "Basque", "euskera"),
    ("be", "Belarusian", "беларуская"),
    ("bn", "Bengali", "বাংলা"),
    ("bn-IN", "Bengali (India)", "বাংলা (ভারত)"),
    ("brx", "Bodo (India)", "बोडो"),
    ("bs", "Bosnian", "Bosanski"),
    ("br", "Breton", "brezhoneg"),
    ("bg", "Bulgarian", "български"),
    ("my", "Burmese", "မန္မာစာ"),
    ("ca", "Catalan", "català"),
    ("ca-valencia", "Catalan (Valencian)", "català (valencià)"),
    ("ckb", "Central Kurdish", "سۆرانی"),
    ("zh-CN", "Chinese (simplified)", "中文 (简体)"),
    ("zh-TW", "Chinese (traditional)", "中文 (正體)"),
    ("hr", "Croatian", "Hrvatski"),
    ("cs", "Czech", "čeština"),
    ("da", "Danish", "dansk"),
    ("dgo", "Dogri", "डोगरी"),
    ("nl", "Dutch", "Nederlands"),
    ("dz", "Dzongkha", "རྫོང་ཁ"),
    ("en-GB", "English (GB)", "English (GB)"),
    ("en-US", "English (US)", "English (US)"),
    ("en-ZA", "English (ZA)", "English (ZA)"),
    ("eo", "Esperanto", "Esperanto"),
    ("et", "Estonian", "eesti keel"),
    ("fi", "Finnish", "suomi"),
    ("fr", "French", "français"),
    ("fy", "Frisian", "Frysk"),
    ("fur", "Friulian", ""),
    ("gl", "Galician", "Galego"),
    ("ka", "Georgian", "ქართული"),
    ("de", "German", "Deutsch"),
    ("el", "Greek", "ελληνικά"),
    ("gug", "Guarani", "avañe'ẽ"),
    ("gu", "Gujarati", "ગુજરાતી"),
    ("he", "Hebrew", "עברית"),
    ("hi", "Hindi", "हिन्दी"),
    ("hu", "Hungarian", "magyar"),
    ("is", "Icelandic", "Íslenska"),
    ("id", "Indonesian", "Bahasa Indonesia"),
    ("ga", "Irish", "Gaeilge"),
    ("it", "Italian", "italiano"),
    ("ja", "Japanese", "日本語"),
    ("kab", "Kabyle", "Taqbaylit"),
    ("kn", "Kannada", "ಕನ್ನಡ"),
    ("ks", "Kashmiri", "ﻚﺸﻤﻳﺮﻳ"),
    ("kk", "Kazakh", "Қазақша"),
    ("km", "Khmer", "ខ្មែរ"),
    ("rw", "Kinyarwanda", "kinyaRwanda"),
    ("kok", "Konkani", "कोंकणी"),
    ("ko", "Korean", "한국어 [韓國語]"),
    ("kmr-Latn", "Kurdish (latin script)", "Kurdish (latin script)"),
    ("lo", "Lao", "ພາສາລາວ"),
    ("lv", "Latvian", "latviešu"),
    ("lt", "Lithuanian", "Lietuvių kalba"),
    ("dsb", "Lower Sorbian", "Dolnoserbšćina"),
    ("lb", "Luxembourgish", "Lëtzebuergesch"),
    ("mk", "Macedonian", "македонски"),
    ("mai", "Maithili", "मैथिली"),
    ("ml", "Malayalam", "മലയാളം"),
    ("mni", "Manipuri", "মৈইতৈইলোন"),
    ("mr", "Marathi", "मराठी"),
    ("mn", "Mongolian", "монгол"),
    ("nr", "Ndebele (South)", "Ndébélé"),
    ("ne", "Nepali", "नेपाली"),
    ("nso", "Northern Sotho", "Sesotho sa Leboa"),
    ("nb", "Norwegian Bokmål", "Bokmål"),
    ("nn", "Norwegian Nynorsk", "Nynorsk"),
    ("oc", "Occitan", "occitan"),
    ("or", "Oriya", "ଓଡ଼ିଆ"),
    ("om", "Oromo", "Afaan Oromo"),
    ("pa-IN", "Panjabi", "ਪੰਜਾਬੀ"),
    ("fa", "Persian", "فارسى"),
    ("pl", "Polish", "polski"),
    ("pt", "Portuguese", "português"),
    ("pt-BR", "Portuguese (Brazil)", "português (Brasil)"),
    ("ro", "Romanian", "român"),
    ("ru", "Russian", "Русский"),
    ("sa-IN", "Sanskrit", "संस्कृतम्"),
    ("sat", "Santali", "संथाली"),
    ("gd", "Scottish Gaelic", "Gàidhlig"),
    ("sr", "Serbian", "српски"),
    ("sr-Latn", "Serbian (latin script)", "srpski latinicom"),
    ("sid", "Sidama", ""),
    ("szl", "Silesian", "Ślōnski"),
    ("sd", "Sindhi", "ﺲﻧﺩھی"),
    ("si", "Sinhala", "සිංහල"),
    ("sk", "Slovak", "slovenčina"),
    ("sl", "Slovenian", "slovenski"),
    ("st", "Southern Sotho", "Sesotho"),
    ("es", "Spanish", "español"),
    ("ss", "Swati", "siSwati"),
    ("sv", "Swedish", "Svenska"),
    ("tl", "Tagalog", "Tagalog"),
    ("tg", "Tajik", "тоҷикӣ"),
    ("ta", "Tamil", "தமிழ்"),
    ("tt", "Tatar", "татар теле"),
    ("te", "Telugu", "తెలుగు"),
    ("th", "Thai", "ภาษาไทย"),
    ("bo", "Tibetan", "བོད་ཡིག"),
    ("ts", "Tsonga", "xiTshonga"),
    ("tn", "Tswana", "seTswana"),
    ("tr", "Turkish", "Türkçe"),
    ("ug", "Uighur", "ﺉۇﻲﻏۇﺭچە"),
    ("uk", "Ukrainian", "Українська"),
    ("hsb", "Upper Sorbian", "Hornjoserbšćina"),
    ("uz", "Uzbek", "ўзбек"),
    ("ve", "Venda", "tshiVenḓa"),
    ("vec", "Venetian", "Veneto"),
    ("vi", "Vietnamese", "tiếng việt"),
    ("cy", "Welsh", "Welsh/Cymraeg"),
    ("xh", "Xhosa", "isiXhosa"),
    ("zu", "Zulu", "isiZulu"),
]

# Extracted from Opus https://huggingface.co/spaces/igortamara/opus-translate
# 2025-09-04
original_source_langs = [
    "aav",
    "af",
    "afa",
    "alv",
    "ar",
    "art",
    "ase",
    "az",
    "bat",
    "bcl",
    "bem",
    "ber",
    "bg",
    "bi",
    "bn",
    "bnt",
    "bzs",
    "ca",
    "cau",
    "ccs",
    "ceb",
    "cel",
    "chk",
    "cpf",
    "cpp",
    "crs",
    "cs",
    "cus",
    "cy",
    "da",
    "de",
    "dra",
    "ee",
    "efi",
    "eo",
    "es",
    "et",
    "eu",
    "euq",
    "fi",
    "fiu",
    "fj",
    "fr",
    "ga",
    "gaa",
    "gem",
    "gil",
    "gl",
    "gmq",
    "gmw",
    "grk",
    "guw",
    "gv",
    "ha",
    "hi",
    "hil",
    "ho",
    "ht",
    "hu",
    "hy",
    "id",
    "ig",
    "iir",
    "ilo",
    "inc",
    "ine",
    "is",
    "iso",
    "it",
    "itc",
    "ja",
    "jap",
    "ka",
    "kab",
    "kg",
    "kj",
    "kl",
    "ko",
    "kqn",
    "kwn",
    "kwy",
    "lg",
    "ln",
    "loz",
    "lu",
    "lua",
    "lue",
    "lun",
    "luo",
    "lus",
    "lv",
    "mfe",
    "mg",
    "mh",
    "mk",
    "mkh",
    "ml",
    "mos",
    "mr",
    "mt",
    "mul",
    "ng",
    "nic",
    "niu",
    "nl",
    "nso",
    "ny",
    "nyk",
    "om",
    "pa",
    "pag",
    "pap",
    "phi",
    "pis",
    "pl",
    "pon",
    "pqe",
    "rn",
    "rnd",
    "roa",
    "ru",
    "run",
    "rw",
    "sal",
    "sem",
    "sg",
    "sk",
    "sla",
    "sm",
    "sn",
    "sq",
    "srn",
    "ss",
    "st",
    "sv",
    "swc",
    "taw",
    "th",
    "ti",
    "tiv",
    "tl",
    "tll",
    "tn",
    "to",
    "toi",
    "tpi",
    "tr",
    "trk",
    "ts",
    "tum",
    "tvl",
    "uk",
    "umb",
    "ur",
    "urj",
    "ve",
    "vi",
    "wa",
    "wal",
    "war",
    "wls",
    "xh",
    "yap",
    "yo",
    "zh",
    "zle",
    "zls",
    "zlw",
]


def extract_pairs():
    """
    Helper function to match languages present in LibreOffice and Opus
    translation
    """
    return [i for i in original_lo_list if i[0] in original_source_langs]


def _sse_iter(url, data=None, headers=None, timeout=30):
    """Iterate over SSE data payloads from a URL using only stdlib urllib.request.
    Mirrors the SSE parsing logic in core/api.py's _run_streaming_loop."""
    import urllib.request
    from plugin.framework.constants import USER_AGENT, APP_REFERER, APP_TITLE

    if headers is None:
        headers = {}
    if "User-Agent" not in headers:
        headers["User-Agent"] = USER_AGENT
    if "HTTP-Referer" not in headers:
        headers["HTTP-Referer"] = APP_REFERER
    if "X-Title" not in headers:
        headers["X-Title"] = APP_TITLE

    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as stream:
        from plugin.modules.http.client import iterate_sse
        for payload in iterate_sse(stream):
            if payload == "[DONE]":
                break
            yield payload


def opustm_hf_translate(
    text: str, src_language: str, target_language: str = "English"
) -> str:
    """Translate text using the OpusTM model hosted on Hugging Face Spaces.
    Uses core.api.sync_request for consistent headers."""
    # Step 1: POST to get an event_id for the streaming result
    post_data = json.dumps({"data": [text, src_language, target_language]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    resp_data = sync_request(API_TRANSLATE_GRADIO, data=post_data, headers=headers)
    event_id = resp_data["event_id"]

    # Step 2: GET the SSE stream for that event_id and collect all data payloads
    stream_url = API_TRANSLATE_GRADIO + "/" + event_id
    result = ""
    for payload in _sse_iter(stream_url, headers={"Accept": "text/event-stream"}):
        result += payload

    return json.loads(result)[0]
