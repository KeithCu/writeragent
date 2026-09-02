# WriterAgent - AI Writing Assistant for LibreOffice
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

"""Internationalization (i18n) utility for WriterAgent.

Uses standard Python gettext to localize strings dynamically.

Concurrency: gettext catalogs are loaded once on the LibreOffice UI
thread in ``init_i18n`` (reads the office locale from UNO config). After
that, ``_()`` is a read of the loaded catalog — no lock. If something
calls ``get_lo_locale`` from a **background** thread before init, it
returns English (``en_US``) instead of calling
``uno.getComponentContext()``, which can create a second, wrong UNO
context and break dialogs.
"""

import os
import gettext
import logging
from typing import Any, Optional, cast

from plugin.framework.constants import get_locales_dir

log = logging.getLogger("writeragent.i18n")

# Set by init_i18n(); always non-None after init_i18n() returns.
_translation: Optional[gettext.NullTranslations] = None
# LO UI locale tag used for the loaded catalog (for fluff-word cache keys).
_active_locale: str = "en_US"

# When UNO cannot supply ooLocale (tests, early init), use English catalogs.
_DEFAULT_LOCALE = "en_US"


from plugin.framework.deal_shim import DEAL_MAX_MSGID, str_bounded, deal

@deal.post(lambda result: isinstance(result, str) and len(result) > 0)
def get_active_locale() -> str:
    """Return the locale tag for the gettext catalog loaded by init_i18n()."""
    return _active_locale


def get_lo_locale(ctx=None):
    """Return the LibreOffice UI locale from configuration only (no OS LANG).

    Reads ``/org.openoffice.Setup/L10N`` → ``ooLocale``. On failure or empty
    value, returns ``en_US`` so gettext still loads a predictable catalog.
    """
    # crosshair: off
    try:
        import uno

        if ctx is None:
            from plugin.framework.thread_guard import on_main_thread

            # Off-main (e.g. ``_()`` before init_i18n): do not start a new UNO
            # infection via uno.getComponentContext(); English catalog is fine.
            if not on_main_thread():
                return _DEFAULT_LOCALE
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx()
        smgr = cast("Any", ctx).getServiceManager()
        config_provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
        ca = config_provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="nodepath", Value="/org.openoffice.Setup/L10N"),))

        locale = ca.getPropertyValue("ooLocale")
        if locale:
            if "Mock" in str(type(locale)):
                return _DEFAULT_LOCALE
            # Setup.xcs: ooLocale is xs:string (e.g. "ja", "ja-JP").
            # LibreOffice often uses a hyphen; gettext prefers an underscore.
            return locale.replace("-", "_")
    except Exception as e:
        log.debug("Failed to determine LibreOffice locale: %s", e)

    return _DEFAULT_LOCALE


def _mo_candidates(localedir: str, lang: str, domain: str = "writeragent") -> list[str]:
    # crosshair: off  # path join / locale stem combinatorics (cover-all 33569420452: 14810 examples / ~48m module with load_translation). Doable later: closed lang tag set.
    """Likely ``.mo`` paths for *lang* (exact tag, then language stem)."""
    tags = [lang]
    stem = lang.split(".")[0]
    if stem not in tags:
        tags.append(stem)
    if "_" in stem:
        prefix = stem.split("_", 1)[0]
        if prefix not in tags:
            tags.append(prefix)
    return [os.path.join(localedir, tag, "LC_MESSAGES", "%s.mo" % domain) for tag in tags]


def load_translation(
    languages: list[str],
    localedir: str | None = None,
    *,
    fallback: bool = True,
) -> gettext.NullTranslations:
    # crosshair: off  # gettext.find + open(.mo) filesystem (cover-all 33569420452: 13456 examples). Engine-hostile; doable later with injected catalog.
    """Load domain ``writeragent`` for *languages*.

    ``gettext.find`` expands tags via ``locale.normalize``. On Windows that
    search can miss ``locales/<lang>/LC_MESSAGES/writeragent.mo`` even when
    the file exists (CI 33453184665: ``Built-in`` stayed English). Open the
    explicit catalog path when the stdlib lookup returns a dummy catalog.
    """
    locales_dir = localedir if localedir is not None else get_locales_dir()
    trans = gettext.translation(
        "writeragent",
        locales_dir,
        languages=languages,
        fallback=True,
    )
    # GNUTranslations subclasses NullTranslations — compare the exact type.
    if type(trans) is not gettext.NullTranslations:
        return trans
    for lang in languages:
        for path in _mo_candidates(locales_dir, lang):
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                return gettext.GNUTranslations(fh)
    if fallback:
        return gettext.NullTranslations()
    raise FileNotFoundError(
        "no writeragent.mo for languages %s under %s" % (languages, locales_dir)
    )


def init_i18n(ctx=None) -> None:
    """Load gettext for the current locale.

    Always sets :data:`_translation` before return (``NullTranslations`` on any
    failure so callers never see ``None`` after a successful call).
    """
    # crosshair: off
    global _translation, _active_locale

    if _translation is not None:
        return

    try:
        locale = get_lo_locale(ctx)
        _active_locale = locale
        locales_dir = get_locales_dir()
        mofiles = gettext.find("writeragent", localedir=locales_dir, languages=[locale], all=True)
        if not mofiles:
            mofiles = []

        log.debug("i18n init: ctx_is_none=%s locale=%s locales_dir=%s (exists=%s) mofiles=%s", ctx is None, locale, locales_dir, os.path.isdir(locales_dir), mofiles if mofiles else "none")

        _translation = load_translation([locale], locales_dir, fallback=True)
        log.debug("i18n init: translation_type=%s", type(_translation).__name__)
    except Exception as e:
        log.debug("Failed to initialize i18n: %s. Falling back to default gettext.", e)
        _active_locale = _DEFAULT_LOCALE
        _translation = gettext.NullTranslations()
        log.debug("i18n init: translation_type=%s", type(_translation).__name__)


# Msgids must be <= DEAL_MAX_MSGID (wide in both profiles; SOURCE is 8192/16).
# CrossHair-on with msgid=1024 wanders for hours. Shrinking DEAL_MAX_MSGID would
# reject real UI _() strings at import under WRITERAGENT_CROSSHAIR=1. Pytest
# still checks the contract; check-all skips this wrapper.
@deal.pre(lambda message: str_bounded(message, DEAL_MAX_MSGID))
@deal.post(lambda result: isinstance(result, str))
def _(message: str) -> str:
    """Translate English msgid *message* via gettext. Must be :class:`str`."""
    # crosshair: off
    if not isinstance(message, str):
        raise TypeError("gettext msgid must be str")

    global _translation
    if _translation is None:
        init_i18n()

    assert _translation is not None
    return _translation.gettext(message)
