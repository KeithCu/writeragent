# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Text-to-Speech (TTS) synthesis and playback service for WriterAgent."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable

from plugin.framework.config import get_config, get_config_str, set_config, get_api_key_for_endpoint
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.sandbox import resolve_venv_python

log = logging.getLogger(__name__)

_active_speech_proc: subprocess.Popen[Any] | None = None
_speech_active: bool = False
_speech_cancelled = threading.Event()
_speech_lock = threading.Lock()


def is_speaking() -> bool:
    """Return True if TTS speech synthesis or playback is actively occurring."""
    with _speech_lock:
        return _speech_active or (_active_speech_proc is not None and _active_speech_proc.poll() is None)


def clean_text_for_speech(text: str) -> str:
    """Prepare text for spoken synthesis by stripping markdown, code, and noise.
    
    Removes fenced code blocks, raw URLs, excessive markup, and normalizes
    whitespace so speech output sounds natural and fluent.
    """
    if not text:
        return ""

    # Remove code blocks ```...```
    cleaned = re.sub(r"```[\s\S]*?```", " [code block omitted] ", text)

    # Remove inline code `...`
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)

    # Remove markdown images ![alt](url)
    cleaned = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "", cleaned)

    # Replace markdown links [text](url) with just text
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cleaned)

    # Remove markdown headers #, ##, etc.
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)

    # Remove bold/italic markers
    cleaned = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", cleaned)

    # Remove HTML/XML tags
    cleaned = re.sub(r"<[^>]+>", "", cleaned)

    # Remove raw URLs
    cleaned = re.sub(r"https?://\S+", "link", cleaned)

    # Normalize whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned


def stop_speech() -> None:
    """Immediately stop and cancel any active speech synthesis or playback."""
    global _active_speech_proc, _speech_active
    with _speech_lock:
        was_active = _speech_active or _active_speech_proc is not None
        _speech_active = False
        if was_active:
            _speech_cancelled.set()
        else:
            _speech_cancelled.clear()
        if _active_speech_proc is not None:
            try:
                log.info("Terminating active speech playback process (PID %s)", _active_speech_proc.pid)
                _active_speech_proc.terminate()
                _active_speech_proc.poll()
            except Exception as e:
                log.debug("stop_speech terminate error: %s", e)
            finally:
                _active_speech_proc = None


# Curated Piper ONNX voice models covering all 34 WriterAgent locales from HuggingFace
# Schema: voice_id -> (onnx_relative_path, json_relative_path, lang_code, display_label)
_PIPER_VOICE_MODELS: dict[str, tuple[str, str, str, str]] = {
    # English (US & UK)
    "en_US-lessac-medium": (
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
        "en_US",
        "en_US-lessac-medium (US English Female - Lessac)",
    ),
    "en_US-amy-medium": (
        "en/en_US/amy/medium/en_US-amy-medium.onnx",
        "en/en_US/amy/medium/en_US-amy-medium.onnx.json",
        "en_US",
        "en_US-amy-medium (US English Female - Amy)",
    ),
    "en_US-ryan-medium": (
        "en/en_US/ryan/medium/en_US-ryan-medium.onnx",
        "en/en_US/ryan/medium/en_US-ryan-medium.onnx.json",
        "en_US",
        "en_US-ryan-medium (US English Male - Ryan)",
    ),
    "en_US-danny-low": (
        "en/en_US/danny/low/en_US-danny-low.onnx",
        "en/en_US/danny/low/en_US-danny-low.onnx.json",
        "en_US",
        "en_US-danny-low (US English Male - Danny)",
    ),
    "en_GB-alan-medium": (
        "en/en_GB/alan/medium/en_GB-alan-medium.onnx",
        "en/en_GB/alan/medium/en_GB-alan-medium.onnx.json",
        "en_GB",
        "en_GB-alan-medium (UK English Male - Alan)",
    ),
    "en_GB-alba-medium": (
        "en/en_GB/alba/medium/en_GB-alba-medium.onnx",
        "en/en_GB/alba/medium/en_GB-alba-medium.onnx.json",
        "en_GB",
        "en_GB-alba-medium (UK English Female - Alba)",
    ),
    # German (de)
    "de_DE-thorsten-medium": (
        "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx",
        "de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json",
        "de_DE",
        "de_DE-thorsten-medium (German Male - Thorsten)",
    ),
    "de_DE-thorsten_emotional-medium": (
        "de/de_DE/thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx",
        "de/de_DE/thorsten_emotional/medium/de_DE-thorsten_emotional-medium.onnx.json",
        "de_DE",
        "de_DE-thorsten_emotional-medium (German Male - Thorsten Emotional)",
    ),
    # French (fr)
    "fr_FR-siwis-medium": (
        "fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx",
        "fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx.json",
        "fr_FR",
        "fr_FR-siwis-medium (French Female - Siwis)",
    ),
    "fr_FR-upmc-medium": (
        "fr/fr_FR/upmc/medium/fr_FR-upmc-medium.onnx",
        "fr/fr_FR/upmc/medium/fr_FR-upmc-medium.onnx.json",
        "fr_FR",
        "fr_FR-upmc-medium (French Female - UPMC)",
    ),
    # Spanish (es)
    "es_ES-davefx-medium": (
        "es/es_ES/davefx/medium/es_ES-davefx-medium.onnx",
        "es/es_ES/davefx/medium/es_ES-davefx-medium.onnx.json",
        "es_ES",
        "es_ES-davefx-medium (Spanish Male - Davefx)",
    ),
    "es_ES-sharvard-medium": (
        "es/es_ES/sharvard/medium/es_ES-sharvard-medium.onnx",
        "es/es_ES/sharvard/medium/es_ES-sharvard-medium.onnx.json",
        "es_ES",
        "es_ES-sharvard-medium (Spanish Male - Sharvard)",
    ),
    # Italian (it)
    "it_IT-paola-medium": (
        "it/it_IT/paola/medium/it_IT-paola-medium.onnx",
        "it/it_IT/paola/medium/it_IT-paola-medium.onnx.json",
        "it_IT",
        "it_IT-paola-medium (Italian Female - Paola)",
    ),
    # Dutch (nl)
    "nl_NL-mls-medium": (
        "nl/nl_NL/mls/medium/nl_NL-mls-medium.onnx",
        "nl/nl_NL/mls/medium/nl_NL-mls-medium.onnx.json",
        "nl_NL",
        "nl_NL-mls-medium (Dutch Female - MLS)",
    ),
    "nl_BE-nathalie-medium": (
        "nl/nl_BE/nathalie/medium/nl_BE-nathalie-medium.onnx",
        "nl/nl_BE/nathalie/medium/nl_BE-nathalie-medium.onnx.json",
        "nl_BE",
        "nl_BE-nathalie-medium (Dutch Belgian Female - Nathalie)",
    ),
    # Polish (pl)
    "pl_PL-darkman-medium": (
        "pl/pl_PL/darkman/medium/pl_PL-darkman-medium.onnx",
        "pl/pl_PL/darkman/medium/pl_PL-darkman-medium.onnx.json",
        "pl_PL",
        "pl_PL-darkman-medium (Polish Male - Darkman)",
    ),
    # Portuguese (pt)
    "pt_BR-cadu-medium": (
        "pt/pt_BR/cadu/medium/pt_BR-cadu-medium.onnx",
        "pt/pt_BR/cadu/medium/pt_BR-cadu-medium.onnx.json",
        "pt_BR",
        "pt_BR-cadu-medium (Portuguese Male - Cadu)",
    ),
    "pt_BR-faber-medium": (
        "pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx",
        "pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json",
        "pt_BR",
        "pt_BR-faber-medium (Portuguese Male - Faber)",
    ),
    # Russian (ru)
    "ru_RU-denis-medium": (
        "ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx",
        "ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx.json",
        "ru_RU",
        "ru_RU-denis-medium (Russian Male - Denis)",
    ),
    "ru_RU-dmitri-medium": (
        "ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx",
        "ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx.json",
        "ru_RU",
        "ru_RU-dmitri-medium (Russian Male - Dmitri)",
    ),
    # Chinese (zh_CN, zh_TW)
    "zh_CN-huayan-medium": (
        "zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx",
        "zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json",
        "zh_CN",
        "zh_CN-huayan-medium (Chinese Female - Huayan)",
    ),
    # Japanese (ja)
    "ja_JP-hi_fi_captain-medium": (
        "ja/ja_JP/hi_fi_captain/medium/ja_JP-hi_fi_captain-medium.onnx",
        "ja/ja_JP/hi_fi_captain/medium/ja_JP-hi_fi_captain-medium.onnx.json",
        "ja_JP",
        "ja_JP-hi_fi_captain-medium (Japanese Male - Hi-Fi Captain)",
    ),
    # Korean (ko)
    "ko_KR-kss-medium": (
        "ko/ko_KR/kss/medium/ko_KR-kss-medium.onnx",
        "ko/ko_KR/kss/medium/ko_KR-kss-medium.onnx.json",
        "ko_KR",
        "ko_KR-kss-medium (Korean Female - KSS)",
    ),
    # Turkish (tr)
    "tr_TR-dfki-medium": (
        "tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx",
        "tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx.json",
        "tr_TR",
        "tr_TR-dfki-medium (Turkish Male - DFKI)",
    ),
    # Ukrainian (uk)
    "uk_UA-ukrainian_tts-medium": (
        "uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx",
        "uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx.json",
        "uk_UA",
        "uk_UA-ukrainian_tts-medium (Ukrainian Female - UKR TTS)",
    ),
    # Czech (cs)
    "cs_CZ-jirka-medium": (
        "cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium.onnx",
        "cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium.onnx.json",
        "cs_CZ",
        "cs_CZ-jirka-medium (Czech Male - Jirka)",
    ),
    # Danish (da)
    "da_DK-talesyntese-medium": (
        "da/da_DK/talesyntese/medium/da_DK-talesyntese-medium.onnx",
        "da/da_DK/talesyntese/medium/da_DK-talesyntese-medium.onnx.json",
        "da_DK",
        "da_DK-talesyntese-medium (Danish Male - Talesyntese)",
    ),
    # Finnish (fi)
    "fi_FI-harri-medium": (
        "fi/fi_FI/harri/medium/fi_FI-harri-medium.onnx",
        "fi/fi_FI/harri/medium/fi_FI-harri-medium.onnx.json",
        "fi_FI",
        "fi_FI-harri-medium (Finnish Male - Harri)",
    ),
    # Swedish (sv)
    "sv_SE-nst-medium": (
        "sv/sv_SE/nst/medium/sv_SE-nst-medium.onnx",
        "sv/sv_SE/nst/medium/sv_SE-nst-medium.onnx.json",
        "sv_SE",
        "sv_SE-nst-medium (Swedish Female - NST)",
    ),
    # Greek (el)
    "el_GR-rapunzelina-medium": (
        "el/el_GR/rapunzelina/medium/el_GR-rapunzelina-medium.onnx",
        "el/el_GR/rapunzelina/medium/el_GR-rapunzelina-medium.onnx.json",
        "el_GR",
        "el_GR-rapunzelina-medium (Greek Female - Rapunzelina)",
    ),
    # Hungarian (hu)
    "hu_HU-anna-medium": (
        "hu/hu_HU/anna/medium/hu_HU-anna-medium.onnx",
        "hu/hu_HU/anna/medium/hu_HU-anna-medium.onnx.json",
        "hu_HU",
        "hu_HU-anna-medium (Hungarian Female - Anna)",
    ),
    # Romanian (ro)
    "ro_RO-mihai-medium": (
        "ro/ro_RO/mihai/medium/ro_RO-mihai-medium.onnx",
        "ro/ro_RO/mihai/medium/ro_RO-mihai-medium.onnx.json",
        "ro_RO",
        "ro_RO-mihai-medium (Romanian Male - Mihai)",
    ),
    # Bulgarian (bg)
    "bg_BG-dimitar-medium": (
        "bg/bg_BG/dimitar/medium/bg_BG-dimitar-medium.onnx",
        "bg/bg_BG/dimitar/medium/bg_BG-dimitar-medium.onnx.json",
        "bg_BG",
        "bg_BG-dimitar-medium (Bulgarian Male - Dimitar)",
    ),
    # Slovak (sk)
    "sk_SK-lili-medium": (
        "sk/sk_SK/lili/medium/sk_SK-lili-medium.onnx",
        "sk/sk_SK/lili/medium/sk_SK-lili-medium.onnx.json",
        "sk_SK",
        "sk_SK-lili-medium (Slovak Female - Lili)",
    ),
    # Estonian (et)
    "et_EE-news-medium": (
        "et/et_EE/news/medium/et_EE-news-medium.onnx",
        "et/et_EE/news/medium/et_EE-news-medium.onnx.json",
        "et_EE",
        "et_EE-news-medium (Estonian Female - News)",
    ),
    # Lithuanian (lt)
    "lt_LT-reginute1-medium": (
        "lt/lt_LT/reginute1/medium/lt_LT-reginute1-medium.onnx",
        "lt/lt_LT/reginute1/medium/lt_LT-reginute1-medium.onnx.json",
        "lt_LT",
        "lt_LT-reginute1-medium (Lithuanian Female - Reginute)",
    ),
    # Latvian (lv)
    "lv_LV-aivars-medium": (
        "lv/lv_LV/aivars/medium/lv_LV-aivars-medium.onnx",
        "lv/lv_LV/aivars/medium/lv_LV-aivars-medium.onnx.json",
        "lv_LV",
        "lv_LV-aivars-medium (Latvian Male - Aivars)",
    ),
    # Croatian / Slovenian (hr / sl)
    "sl_SI-artur-medium": (
        "sl/sl_SI/artur/medium/sl_SI-artur-medium.onnx",
        "sl/sl_SI/artur/medium/sl_SI-artur-medium.onnx.json",
        "sl_SI",
        "sl_SI-artur-medium (Slovenian/Croatian Male - Artur)",
    ),
    # Norwegian (nb_NO / nn_NO / no_NO)
    "no_NO-talesyntese-medium": (
        "no/no_NO/talesyntese/medium/no_NO-talesyntese-medium.onnx",
        "no/no_NO/talesyntese/medium/no_NO-talesyntese-medium.onnx.json",
        "no_NO",
        "no_NO-talesyntese-medium (Norwegian Male - Talesyntese)",
    ),
    # Catalan (ca)
    "ca_ES-upc_ona-medium": (
        "ca/ca_ES/upc_ona/medium/ca_ES-upc_ona-medium.onnx",
        "ca/ca_ES/upc_ona/medium/ca_ES-upc_ona-medium.onnx.json",
        "ca_ES",
        "ca_ES-upc_ona-medium (Catalan Female - UPC Ona)",
    ),
    # Indonesian (id)
    "id_ID-news_tts-medium": (
        "id/id_ID/news_tts/medium/id_ID-news_tts-medium.onnx",
        "id/id_ID/news_tts/medium/id_ID-news_tts-medium.onnx.json",
        "id_ID",
        "id_ID-news_tts-medium (Indonesian Female - News TTS)",
    ),
    # Hindi (hi_IN)
    "hi_IN-pratham-medium": (
        "hi/hi_IN/pratham/medium/hi_IN-pratham-medium.onnx",
        "hi/hi_IN/pratham/medium/hi_IN-pratham-medium.onnx.json",
        "hi_IN",
        "hi_IN-pratham-medium (Hindi Male - Pratham)",
    ),
    # Bengali (bn_IN)
    "bn_BD-google-medium": (
        "bn/bn_BD/google/medium/bn_BD-google-medium.onnx",
        "bn/bn_BD/google/medium/bn_BD-google-medium.onnx.json",
        "bn_BD",
        "bn_BD-google-medium (Bengali Female - Google)",
    ),
    # Urdu (ur_PK)
    "ur_PK-aegis_female-medium": (
        "ur/ur_PK/aegis_female/medium/ur_PK-aegis_female-medium.onnx",
        "ur/ur_PK/aegis_female/medium/ur_PK-aegis_female-medium.onnx.json",
        "ur_PK",
        "ur_PK-aegis_female-medium (Urdu Female - Aegis)",
    ),
}

_LOCALE_TO_PIPER_DEFAULT: dict[str, str] = {
    "bg": "bg_BG-dimitar-medium",
    "bn": "bn_BD-google-medium",
    "ca": "ca_ES-upc_ona-medium",
    "cs": "cs_CZ-jirka-medium",
    "da": "da_DK-talesyntese-medium",
    "de": "de_DE-thorsten-medium",
    "el": "el_GR-rapunzelina-medium",
    "en": "en_US-lessac-medium",
    "es": "es_ES-davefx-medium",
    "et": "et_EE-news-medium",
    "fi": "fi_FI-harri-medium",
    "fr": "fr_FR-siwis-medium",
    "hi": "hi_IN-pratham-medium",
    "hr": "sl_SI-artur-medium",
    "hu": "hu_HU-anna-medium",
    "id": "id_ID-news_tts-medium",
    "it": "it_IT-paola-medium",
    "ja": "ja_JP-hi_fi_captain-medium",
    "ko": "ko_KR-kss-medium",
    "lt": "lt_LT-reginute1-medium",
    "lv": "lv_LV-aivars-medium",
    "nb": "no_NO-talesyntese-medium",
    "nn": "no_NO-talesyntese-medium",
    "no": "no_NO-talesyntese-medium",
    "nl": "nl_NL-mls-medium",
    "pl": "pl_PL-darkman-medium",
    "pt": "pt_BR-cadu-medium",
    "ro": "ro_RO-mihai-medium",
    "ru": "ru_RU-denis-medium",
    "sk": "sk_SK-lili-medium",
    "sl": "sl_SI-artur-medium",
    "sv": "sv_SE-nst-medium",
    "tr": "tr_TR-dfki-medium",
    "uk": "uk_UA-ukrainian_tts-medium",
    "ur": "ur_PK-aegis_female-medium",
    "zh": "zh_CN-huayan-medium",
}

_KOKORO_CATALOG_ITEMS: list[dict[str, str]] = [
    # English (US)
    {"value": "af_bella", "label": "af_bella (Kokoro US Female - Bella)", "lang": "en"},
    {"value": "af_heart", "label": "af_heart (Kokoro US Female - Heart)", "lang": "en"},
    {"value": "af_sarah", "label": "af_sarah (Kokoro US Female - Sarah)", "lang": "en"},
    {"value": "af_sky", "label": "af_sky (Kokoro US Female - Sky)", "lang": "en"},
    {"value": "am_adam", "label": "am_adam (Kokoro US Male - Adam)", "lang": "en"},
    {"value": "am_michael", "label": "am_michael (Kokoro US Male - Michael)", "lang": "en"},
    # English (UK)
    {"value": "bf_emma", "label": "bf_emma (Kokoro UK Female - Emma)", "lang": "en"},
    {"value": "bf_isabella", "label": "bf_isabella (Kokoro UK Female - Isabella)", "lang": "en"},
    {"value": "bm_george", "label": "bm_george (Kokoro UK Male - George)", "lang": "en"},
    {"value": "bm_lewis", "label": "bm_lewis (Kokoro UK Male - Lewis)", "lang": "en"},
    # Spanish
    {"value": "ef_dora", "label": "ef_dora (Kokoro Spanish Female - Dora)", "lang": "es"},
    {"value": "em_alex", "label": "em_alex (Kokoro Spanish Male - Alex)", "lang": "es"},
    {"value": "em_santa", "label": "em_santa (Kokoro Spanish Male - Santa)", "lang": "es"},
    # French
    {"value": "ff_siwis", "label": "ff_siwis (Kokoro French Female - Siwis)", "lang": "fr"},
    # Italian
    {"value": "if_sara", "label": "if_sara (Kokoro Italian Female - Sara)", "lang": "it"},
    {"value": "im_nicola", "label": "im_nicola (Kokoro Italian Male - Nicola)", "lang": "it"},
    # Japanese
    {"value": "jf_alpha", "label": "jf_alpha (Kokoro Japanese Female - Alpha)", "lang": "ja"},
    {"value": "jf_gongitsune", "label": "jf_gongitsune (Kokoro Japanese Female - Gongitsune)", "lang": "ja"},
    {"value": "jm_kumo", "label": "jm_kumo (Kokoro Japanese Male - Kumo)", "lang": "ja"},
    # Chinese
    {"value": "zf_xiaobei", "label": "zf_xiaobei (Kokoro Chinese Female - Xiaobei)", "lang": "zh"},
    {"value": "zm_yunjian", "label": "zm_yunjian (Kokoro Chinese Male - Yunjian)", "lang": "zh"},
    # Portuguese
    {"value": "pf_dora", "label": "pf_dora (Kokoro Portuguese Female - Dora)", "lang": "pt"},
    # Hindi
    {"value": "hf_alpha", "label": "hf_alpha (Kokoro Hindi Female - Alpha)", "lang": "hi"},
    {"value": "hf_beta", "label": "hf_beta (Kokoro Hindi Female - Beta)", "lang": "hi"},
    {"value": "hm_omega", "label": "hm_omega (Kokoro Hindi Male - Omega)", "lang": "hi"},
    {"value": "hm_psi", "label": "hm_psi (Kokoro Hindi Male - Psi)", "lang": "hi"},
]

_LOCALE_TO_KOKORO_DEFAULT: dict[str, str] = {
    "en": "af_bella",
    "es": "ef_dora",
    "fr": "ff_siwis",
    "it": "if_sara",
    "ja": "jf_alpha",
    "zh": "zf_xiaobei",
    "hi": "hf_alpha",
    "pt": "pf_dora",
}


def _kokoro_lang_for_voice(voice: str) -> str:
    """Determine the Kokoro phonemizer language code from voice prefix."""
    clean = voice.lower().strip()
    if clean.startswith("b"):
        return "en-gb"
    if clean.startswith("e"):
        return "es"
    if clean.startswith("f"):
        return "fr-fr"
    if clean.startswith("i"):
        return "it"
    if clean.startswith("j"):
        return "ja"
    if clean.startswith("z"):
        return "zh"
    if clean.startswith("h"):
        return "hi"
    if clean.startswith("p"):
        return "pt-br"
    return "en-us"


def get_default_voice_for_locale(family: str, locale: str | None = None) -> str:
    """Return the optimal default voice for a voice family and locale."""
    fam = clean_provider_name(family)
    if locale is None:
        try:
            from plugin.framework.i18n import get_active_locale
            locale = get_active_locale()
        except Exception:
            locale = "en_US"
    stem = (locale or "").split(".")[0].split("_")[0].lower()
    if fam == "piper":
        return _LOCALE_TO_PIPER_DEFAULT.get(stem, "en_US-lessac-medium")
    if fam == "kokoro":
        return _LOCALE_TO_KOKORO_DEFAULT.get(stem, "af_bella")
    if fam in ("openai", "endpoint"):
        return "alloy"
    return "default"


def get_voice_catalog(family: str, locale: str | None = None) -> list[dict[str, str]]:
    """Return ordered voice options for the family, prioritizing the active locale."""
    fam = clean_provider_name(family)
    if fam not in ("piper", "kokoro"):
        return VOICE_CATALOGS.get(fam, [])

    if locale is None:
        try:
            from plugin.framework.i18n import get_active_locale
            locale = get_active_locale()
        except Exception:
            locale = "en_US"

    stem = (locale or "").split(".")[0].split("_")[0].lower()

    if fam == "kokoro":
        locale_voices: list[dict[str, str]] = []
        en_voices: list[dict[str, str]] = []
        other_voices: list[dict[str, str]] = []
        for opt in _KOKORO_CATALOG_ITEMS:
            v_lang = opt.get("lang", "en")
            item = {"value": opt["value"], "label": opt["label"]}
            if stem != "en" and v_lang == stem:
                locale_voices.append(item)
            elif v_lang == "en":
                en_voices.append(item)
            else:
                other_voices.append(item)
        return locale_voices + en_voices + other_voices

    # Piper: matching locale voices first, then English, then others
    locale_voices = []
    en_voices = []
    other_voices = []

    preferred_default = _LOCALE_TO_PIPER_DEFAULT.get(stem)

    for voice_id, (_, _, lang_code, label) in _PIPER_VOICE_MODELS.items():
        v_stem = lang_code.split("_")[0].lower()
        item = {"value": voice_id, "label": label}
        is_loc = stem != "en" and (
            voice_id == preferred_default
            or v_stem == stem
            or (stem in ("nb", "nn") and v_stem == "no")
            or (stem == "hr" and v_stem == "sl")
        )
        if is_loc:
            if voice_id == preferred_default:
                locale_voices.insert(0, item)
            else:
                locale_voices.append(item)
        elif v_stem == "en":
            en_voices.append(item)
        else:
            other_voices.append(item)

    return locale_voices + en_voices + other_voices


VOICE_CATALOGS: dict[str, list[dict[str, str]]] = {
    "kokoro": [
        {"value": opt["value"], "label": opt["label"]} for opt in _KOKORO_CATALOG_ITEMS
    ],
    "piper": [
        {"value": k, "label": v[3]} for k, v in _PIPER_VOICE_MODELS.items()
    ],
    "openai": [
        {"value": "alloy", "label": "alloy (OpenAI Neutral)"},
        {"value": "nova", "label": "nova (OpenAI Warm)"},
        {"value": "shimmer", "label": "shimmer (OpenAI Expressive)"},
        {"value": "echo", "label": "echo (OpenAI Soft Male)"},
        {"value": "onyx", "label": "onyx (OpenAI Deep Male)"},
        {"value": "fable", "label": "fable (OpenAI British Narrator)"},
    ],
    "system": [
        {"value": "default", "label": "default (System Default)"},
    ],
}

DEFAULT_VOICE_FOR_FAMILY: dict[str, str] = {
    "kokoro": "af_bella",
    "piper": "en_US-lessac-medium",
    "openai": "alloy",
    "system": "default",
}


def clean_provider_name(provider_or_label: str) -> str:
    """Normalize provider name or UI label to clean provider code."""
    low = (provider_or_label or "").strip().lower()
    if "kokoro" in low:
        return "kokoro"
    if "piper" in low:
        return "piper"
    if "endpoint" in low:
        return "endpoint"
    return "system"


def clean_voice_name(voice_or_label: str) -> str:
    """Extract canonical voice code from a voice string or UI label."""
    if not voice_or_label:
        return ""
    return voice_or_label.split(" (")[0].strip()


def get_voice_family(provider: str | None, model: str | None = None) -> str:
    """Return voice family key ('kokoro', 'piper', 'openai', 'system')."""
    prov = clean_provider_name(provider or "")
    if prov == "kokoro":
        return "kokoro"
    if prov == "piper":
        return "piper"
    if prov == "endpoint":
        if model and "kokoro" in model.lower():
            return "kokoro"
        return "openai"
    return "system"


def get_scoped_tts_voice(
    provider: str | None = None,
    model: str | None = None,
    locale: str | None = None,
) -> str:
    """Get the scoped voice for the given provider/model's voice family."""
    if provider is None:
        provider = str(get_config("audio.tts_provider") or "system")
    prov_clean = clean_provider_name(provider)
    if model is None and prov_clean == "endpoint":
        try:
            from plugin.framework.client.model_fetcher import get_tts_model
            model = get_tts_model()
        except ImportError:
            model = None

    family = get_voice_family(prov_clean, model)
    scoped_key = f"audio.tts_voice_{family}"
    val = get_config(scoped_key)
    if val and isinstance(val, str) and val.strip():
        return clean_voice_name(val.strip())

    general_voice = str(get_config("audio.tts_voice") or "").strip()
    clean_gen = clean_voice_name(general_voice)
    valid_voices = {opt["value"] for opt in get_voice_catalog(family, locale)}
    if clean_gen in valid_voices:
        return clean_gen

    return get_default_voice_for_locale(family, locale)


def set_scoped_tts_voice(voice: str, provider: str | None = None, model: str | None = None) -> None:
    """Persist the voice selection for the given provider/model family."""
    clean_v = clean_voice_name(voice)
    if not clean_v:
        return
    if provider is None:
        provider = str(get_config("audio.tts_provider") or "system")
    prov_clean = clean_provider_name(provider)
    if model is None and prov_clean == "endpoint":
        try:
            from plugin.framework.client.model_fetcher import get_tts_model
            model = get_tts_model()
        except ImportError:
            model = None

    family = get_voice_family(prov_clean, model)
    scoped_key = f"audio.tts_voice_{family}"
    set_config(scoped_key, clean_v)
    set_config("audio.tts_voice", clean_v)


_KOKORO_VOICES = {
    "alloy": "af_bella",
    "nova": "af_bella",
    "shimmer": "af_sarah",
    "echo": "am_adam",
    "fable": "am_michael",
    "onyx": "am_adam",
}


def parse_tts_speed(val: Any) -> float:
    """Parse speech speed safely, enforcing a minimum of 0.25x."""
    if val is None or val == "":
        return 1.0
    if isinstance(val, (int, float)):
        return max(0.25, min(5.0, float(val)))
    cleaned = str(val).split("(")[0].strip().rstrip("xX").strip().replace(",", ".")
    try:
        speed = float(cleaned)
        return max(0.25, min(5.0, speed))
    except (ValueError, TypeError):
        return 1.0


def _resolve_tts_voice(model: str, voice: str) -> str:
    """Ensure voice name is compatible with the target model."""
    voice = clean_voice_name(voice)
    if not voice:
        voice = "alloy"
    if "kokoro" in model.lower():
        v_low = voice.lower()
        if re.match(r"^[abefhjz][fm]_", v_low):
            return voice
        return _KOKORO_VOICES.get(v_low, "af_bella")
    return voice




def _play_audio_file(file_path: str) -> None:
    """Play an audio file using available OS command-line utilities."""
    global _active_speech_proc
    cmd: list[str] | None = None

    if sys.platform == "darwin":
        cmd = ["afplay", file_path]
    elif sys.platform == "win32":
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f'(New-Object Media.SoundPlayer "{file_path}").PlaySync()',
        ]
    else:
        # Linux / Unix: prioritize players supporting MP3/WAV out-of-the-box
        if shutil.which("ffplay"):
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path]
        elif shutil.which("mpv"):
            cmd = ["mpv", "--no-video", file_path]
        elif shutil.which("mpg123"):
            cmd = ["mpg123", "-q", file_path]
        elif shutil.which("pw-play"):
            cmd = ["pw-play", file_path]
        elif shutil.which("paplay"):
            cmd = ["paplay", file_path]
        elif shutil.which("aplay"):
            cmd = ["aplay", file_path]

    if not cmd:
        log.warning("No audio player found on system to play: %s", file_path)
        return

    try:
        log.info("Playing audio with command: %s", " ".join(cmd))
        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                log.info("Audio playback cancelled before process spawn")
                return
            _active_speech_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        _active_speech_proc.wait()
        log.info("Audio playback completed successfully")
    except Exception as e:
        log.warning("_play_audio_file playback error: %s", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None


def _speak_system(text: str, speed: float = 1.0) -> None:
    """Speak text using built-in OS speech synthesis utilities."""
    global _active_speech_proc
    cmd: list[str] | None = None

    if sys.platform == "darwin":
        # macOS native say command
        rate = int(175 * speed)
        cmd = ["/usr/bin/say", "-r", str(rate), text]
    elif sys.platform == "win32":
        # Windows SAPI via PowerShell
        # Rate is integer from -10 to 10
        rate_int = int((speed - 1.0) * 5)
        rate_int = max(-10, min(10, rate_int))
        escaped = text.replace('"', '`"').replace("'", "''")
        ps_script = (
            f"Add-Type -AssemblyName System.Speech; "
            f"$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$synth.Rate = {rate_int}; "
            f"$synth.Speak('{escaped}')"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps_script]
    else:
        # Linux native
        if shutil.which("spd-say"):
            rate_pct = int((speed - 1.0) * 100)
            rate_pct = max(-100, min(100, rate_pct))
            cmd = ["spd-say", "-r", str(rate_pct), "-w", text]
        elif shutil.which("espeak"):
            speed_wpm = int(160 * speed)
            cmd = ["espeak", "-s", str(speed_wpm), text]

    if not cmd:
        log.warning("No OS native text-to-speech utility (say/spd-say/espeak) found on system.")
        return

    try:
        log.info("Speaking via system command: %s", " ".join(cmd[:3]))
        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                log.info("System speech cancelled before process spawn")
                return
            _active_speech_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        _active_speech_proc.wait()
        log.info("System speech playback completed")
    except Exception as e:
        log.debug("_speak_system error: %s", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None


def _speak_endpoint(text: str, endpoint_url: str, api_key: str, model: str, voice: str, speed: float = 1.0) -> None:
    """Request speech audio from an OpenAI-compatible /audio/speech endpoint."""
    import urllib.request
    import urllib.error
    import json

    # Normalize endpoint URL to /audio/speech
    url = endpoint_url.rstrip("/")
    if not url.endswith("/audio/speech"):
        if url.endswith("/v1"):
            url = f"{url}/audio/speech"
        else:
            url = f"{url}/v1/audio/speech"

    eff_voice = _resolve_tts_voice(model, voice)
    payload = {
        "model": model or "hexgrad/Kokoro-82M",
        "input": text,
        "voice": eff_voice,
        "speed": speed,
        "response_format": "mp3",
    }

    log.info("Requesting TTS from %s (model=%s, voice=%s, text_len=%d)", url, payload["model"], eff_voice, len(text))

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "WriterAgent/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    tmp_file = None
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_bytes = resp.read()

        log.info("TTS audio received from %s (%d bytes)", url, len(audio_bytes))

        if _speech_active and _speech_cancelled.is_set():
            log.info("TTS playback cancelled after download")
            return

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            tmp_file = f.name

        _play_audio_file(tmp_file)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        log.error("TTS HTTP error %d from %s: %s | Response: %s", getattr(e, "code", 0), url, e, err_body)
    except Exception as e:
        log.exception("TTS error from %s: %s", url, e)
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass


def _resolve_kokoro_model_files() -> tuple[str, str]:
    """Resolve paths to Kokoro ONNX model and voices file, downloading if missing."""
    cache_dir = os.path.expanduser("~/.cache/kokoro")
    default_model = os.path.join(cache_dir, "kokoro-v0_19.onnx")
    default_voices = os.path.join(cache_dir, "voices.bin")

    model_path = os.environ.get("KOKORO_MODEL_PATH") or default_model
    voices_path = os.environ.get("KOKORO_VOICES_PATH") or default_voices

    if os.path.exists(model_path) and os.path.exists(voices_path):
        return model_path, voices_path

    try:
        os.makedirs(cache_dir, exist_ok=True)
        import urllib.request
        base_url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files"
        if not os.path.exists(voices_path) and voices_path == default_voices:
            log.info("Downloading Kokoro voices to %s...", default_voices)
            urllib.request.urlretrieve(f"{base_url}/voices.bin", default_voices)
            voices_path = default_voices
        if not os.path.exists(model_path) and model_path == default_model:
            log.info("Downloading Kokoro ONNX model to %s...", default_model)
            urllib.request.urlretrieve(f"{base_url}/kokoro-v0_19.onnx", default_model)
            model_path = default_model
    except Exception as e:
        log.warning("Could not auto-download Kokoro models: %s", e)

    return model_path, voices_path


def _resolve_piper_model_file(voice: str) -> str:
    """Resolve path to Piper ONNX model file, downloading on demand if missing."""
    clean_v = clean_voice_name(voice)
    if not clean_v:
        clean_v = "en_US-lessac-medium"

    if os.path.isabs(clean_v) and os.path.exists(clean_v):
        return clean_v

    cache_dir = os.path.expanduser("~/.cache/piper")
    voice_file = os.path.join(cache_dir, f"{clean_v}.onnx")
    json_file = os.path.join(cache_dir, f"{clean_v}.onnx.json")

    if os.path.exists(voice_file) and os.path.exists(json_file):
        return voice_file

    # If voice is in curated catalog, download on-demand
    if clean_v in _PIPER_VOICE_MODELS:
        rel_onnx, rel_json, _, _ = _PIPER_VOICE_MODELS[clean_v]
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        onnx_url = f"{base_url}/{rel_onnx}"
        json_url = f"{base_url}/{rel_json}"
        try:
            os.makedirs(cache_dir, exist_ok=True)
            import urllib.request
            log.info("Downloading Piper voice model '%s' to %s...", clean_v, voice_file)
            req_onnx = urllib.request.Request(onnx_url, headers={"User-Agent": "WriterAgent/1.0"})
            with urllib.request.urlopen(req_onnx, timeout=60) as resp, open(voice_file, "wb") as f_out:
                shutil.copyfileobj(resp, f_out)
            req_json = urllib.request.Request(json_url, headers={"User-Agent": "WriterAgent/1.0"})
            with urllib.request.urlopen(req_json, timeout=30) as resp, open(json_file, "wb") as f_out:
                shutil.copyfileobj(resp, f_out)
            return voice_file
        except Exception as e:
            log.warning("Could not auto-download Piper voice '%s': %s", clean_v, e)
            if os.path.exists(voice_file):
                try:
                    os.remove(voice_file)
                except Exception:
                    pass
            if os.path.exists(json_file):
                try:
                    os.remove(json_file)
                except Exception:
                    pass

    # Fallback to English default
    default_voice_file = os.path.join(cache_dir, "en_US-lessac-medium.onnx")
    default_json = os.path.join(cache_dir, "en_US-lessac-medium.onnx.json")
    if os.path.exists(default_voice_file) and os.path.exists(default_json):
        return default_voice_file

    try:
        os.makedirs(cache_dir, exist_ok=True)
        import urllib.request
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
        log.info("Downloading Piper default voice model to %s...", default_voice_file)
        urllib.request.urlretrieve(f"{base_url}/en_US-lessac-medium.onnx", default_voice_file)
        urllib.request.urlretrieve(f"{base_url}/en_US-lessac-medium.onnx.json", default_json)
        return default_voice_file
    except Exception as e:
        log.warning("Could not auto-download Piper default voice model: %s", e)

    return voice


def _speak_kokoro_local(text: str, voice: str = "af_bella", speed: float = 1.0) -> None:
    """Synthesize text using local Kokoro ONNX model in configured venv and play audio."""
    global _active_speech_proc
    log.info("Speaking via local Kokoro (voice=%s, speed=%.2f)", voice, speed)
    if _speech_active and _speech_cancelled.is_set():
        return

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not py_exe:
        log.warning(
            "Local Kokoro TTS requires a configured Python venv. "
            "Please configure your venv path in Settings → Python."
        )
        _speak_system(text, speed=speed)
        return

    bin_dir = os.path.dirname(py_exe)
    cand_cli = os.path.join(bin_dir, "kokoro.exe" if sys.platform == "win32" else "kokoro")
    kokoro_cli: str | None = cand_cli if os.path.isfile(cand_cli) and os.access(cand_cli, os.X_OK) else None

    if kokoro_cli:
        tmp_wav = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_wav = f.name
            cmd = [kokoro_cli, "--voice", voice, "--speed", str(speed), "--output", tmp_wav, text]
            with _speech_lock:
                if _speech_active and _speech_cancelled.is_set():
                    return
                _active_speech_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            _active_speech_proc.wait()
            if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 0:
                _play_audio_file(tmp_wav)
                return
        except Exception as e:
            log.warning("Kokoro CLI execution error: %s", e)
        finally:
            with _speech_lock:
                _active_speech_proc = None
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except Exception:
                    pass

    model_path, voices_path = _resolve_kokoro_model_files()
    if not os.path.exists(model_path) or not os.path.exists(voices_path):
        log.warning("Kokoro ONNX model files missing (%s, %s); falling back to OS speech.", model_path, voices_path)
        _speak_system(text, speed=speed)
        return

    lang = _kokoro_lang_for_voice(voice)
    tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name
        script = (
            "import sys\n"
            "from kokoro_onnx import Kokoro\n"
            "import soundfile as sf\n"
            "kokoro = Kokoro(sys.argv[5], sys.argv[6])\n"
            "v = sys.argv[2] if sys.argv[2] in kokoro.voices else ('af_bella' if 'af_bella' in kokoro.voices else kokoro.voices[0])\n"
            "samples, rate = kokoro.create(sys.argv[1], voice=v, speed=float(sys.argv[3]), lang=sys.argv[7])\n"
            "sf.write(sys.argv[4], samples, rate)\n"
        )
        cmd = [py_exe, "-c", script, text, voice, str(speed), tmp_wav, model_path, voices_path, lang]
        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                return
            _active_speech_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
        _, stderr = _active_speech_proc.communicate()
        if _active_speech_proc.returncode != 0:
            log.warning("Venv Kokoro failed (code %d): %s", _active_speech_proc.returncode, stderr)
        elif os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            _play_audio_file(tmp_wav)
            return
    except Exception as e:
        log.warning("Venv Kokoro execution error: %s", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    log.warning(
        "Local Kokoro engine not available in configured venv. "
        "Install with: uv pip install kokoro-onnx soundfile. Falling back to OS speech."
    )
    _speak_system(text, speed=speed)


def _speak_piper_local(text: str, voice: str = "en_US-lessac-medium", speed: float = 1.0) -> None:
    """Synthesize text using local Piper fast neural TTS in configured venv and play audio."""
    global _active_speech_proc
    log.info("Speaking via local Piper (voice=%s, speed=%.2f)", voice, speed)
    if _speech_active and _speech_cancelled.is_set():
        return

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    py_exe = resolve_venv_python(venv_dir) if venv_dir else None
    if not py_exe:
        log.warning(
            "Local Piper TTS requires a configured Python venv. "
            "Please configure your venv path in Settings → Python."
        )
        _speak_system(text, speed=speed)
        return

    model_file = _resolve_piper_model_file(voice)

    bin_dir = os.path.dirname(py_exe)
    cand_bin = os.path.join(bin_dir, "piper.exe" if sys.platform == "win32" else "piper")
    piper_bin: str | None = cand_bin if os.path.isfile(cand_bin) and os.access(cand_bin, os.X_OK) else None

    tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name

        length_scale = round(1.0 / max(0.2, min(5.0, speed)), 2)

        cmd: list[str] | None = None
        if piper_bin:
            cmd = [piper_bin, "--model", model_file, "--length_scale", str(length_scale), "--output_file", tmp_wav]
        else:
            cmd = [py_exe, "-m", "piper", "--model", model_file, "--length_scale", str(length_scale), "--output_file", tmp_wav]

        with _speech_lock:
            if _speech_active and _speech_cancelled.is_set():
                return
            _active_speech_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        try:
            _, stderr = _active_speech_proc.communicate(input=text, timeout=30)
            if _active_speech_proc.returncode != 0:
                log.warning("Piper process failed (code %d): %s", _active_speech_proc.returncode, stderr)
        except Exception:
            _active_speech_proc.kill()
            raise

        if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 0:
            _play_audio_file(tmp_wav)
            return
        else:
            log.warning("Piper produced empty audio for voice %s; falling back to OS speech", voice)
    except FileNotFoundError:
        log.warning(
            "Local Piper executable not found in configured venv. Install via 'uv pip install piper-tts'. "
            "Falling back to OS speech."
        )
    except Exception as e:
        log.warning("Piper synthesis error: %s; falling back to OS speech", e)
    finally:
        with _speech_lock:
            _active_speech_proc = None
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    _speak_system(text, speed=speed)


def speak_text_async(text: str, on_complete: Callable[[], None] | None = None) -> None:
    """Synthesize and speak text in a background thread."""
    global _speech_active
    if not bool(get_config("audio.tts_enabled")):
        log.debug("speak_text_async: TTS is disabled (audio.tts_enabled=False)")
        return

    clean = clean_text_for_speech(text)
    if not clean:
        log.debug("speak_text_async: No speakable text after cleaning")
        return

    with _speech_lock:
        _speech_active = True
        _speech_cancelled.clear()

    log.info("speak_text_async: queued speech for %d chars", len(clean))

    def _worker() -> None:
        global _speech_active
        try:
            if _speech_cancelled.is_set():
                return

            raw_prov = str(get_config("audio.tts_provider") or "system")
            provider = clean_provider_name(raw_prov)
            speed = parse_tts_speed(get_config("audio.tts_speed"))
            model = ""
            if provider == "endpoint":
                from plugin.framework.client.model_fetcher import get_tts_model
                model = get_tts_model() or "hexgrad/Kokoro-82M"

            voice = get_scoped_tts_voice(provider, model)

            log.info("TTS worker executing: provider=%s, speed=%.2f, voice=%s, model=%s", provider, speed, voice, model)

            if provider == "system":
                _speak_system(clean, speed=speed)
            elif provider == "kokoro":
                _speak_kokoro_local(clean, voice=voice, speed=speed)
            elif provider == "piper":
                _speak_piper_local(clean, voice=voice, speed=speed)
            elif provider == "endpoint":
                from plugin.framework.config import get_current_endpoint
                url = get_current_endpoint()
                api_key = get_api_key_for_endpoint(url)

                log.info("TTS endpoint resolved: url=%s, model=%s, has_key=%s", url, model, bool(api_key))

                if url:
                    _speak_endpoint(clean, url, api_key, model=model, voice=voice, speed=speed)
                else:
                    log.warning("No endpoint URL available for TTS; falling back to OS system speech.")
                    _speak_system(clean, speed=speed)
            else:
                _speak_system(clean, speed=speed)
        except Exception as e:
            log.exception("speak_text_async worker error: %s", e)
        finally:
            with _speech_lock:
                _speech_active = False
                _speech_cancelled.clear()
            if on_complete:
                try:
                    on_complete()
                except Exception:
                    pass

    run_in_background(_worker, dedicated=True)
