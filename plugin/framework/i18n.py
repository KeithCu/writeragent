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
"""

import os
import gettext
import logging
from typing import Any, Optional, cast

from plugin.framework.utils import get_plugin_dir

log = logging.getLogger("writeragent.i18n")

# Set by init_i18n(); always non-None after init_i18n() returns.
_translation: Optional[gettext.NullTranslations] = None


# When UNO cannot supply ooLocale (tests, early init), use English catalogs.
_DEFAULT_LOCALE = "en_US"


def get_lo_locale(ctx=None):
    """Return the LibreOffice UI locale from configuration only (no OS LANG).

    Reads ``/org.openoffice.Setup/L10N`` → ``ooLocale``. On failure or empty
    value, returns ``en_US`` so gettext still loads a predictable catalog.
    """
    try:
        import uno
        if ctx is None:
            ctx = uno.getComponentContext()
        smgr = cast(Any, ctx).getServiceManager()
        config_provider = smgr.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", ctx)
        ca = config_provider.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationAccess",
            (uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="nodepath", Value="/org.openoffice.Setup/L10N"),)
        )

        locale = ca.getPropertyValue("ooLocale")
        if locale:
            if "Mock" in str(type(locale)):
                return _DEFAULT_LOCALE
            # LibreOffice often returns "en-US", gettext prefers "en_US"
            return locale.replace("-", "_")
    except Exception as e:
        log.debug("Failed to determine LibreOffice locale: %s", e)

    return _DEFAULT_LOCALE


def init_i18n(ctx=None) -> None:
    """Load gettext for the current locale.

    Always sets :data:`_translation` before return (``NullTranslations`` on any
    failure so callers never see ``None`` after a successful call).
    """
    global _translation

    if _translation is not None:
        return

    try:
        locale = get_lo_locale(ctx)
        locales_dir = os.path.join(get_plugin_dir(), "locales")
        mofiles = gettext.find(
            "writeragent", localedir=locales_dir, languages=[locale], all=True
        )
        if not mofiles:
            mofiles = []

        log.debug(
            "i18n init: ctx_is_none=%s locale=%s locales_dir=%s (exists=%s) "
            "mofiles=%s",
            ctx is None,
            locale,
            locales_dir,
            os.path.isdir(locales_dir),
            mofiles if mofiles else "none",
        )

        _translation = gettext.translation(
            domain="writeragent",
            localedir=locales_dir,
            languages=[locale],
            fallback=True
        )
        log.debug(
            "i18n init: translation_type=%s",
            type(_translation).__name__,
        )
    except Exception as e:
        log.debug("Failed to initialize i18n: %s. Falling back to default gettext.", e)
        _translation = gettext.NullTranslations()
        log.debug("i18n init: translation_type=%s", type(_translation).__name__)


def _(message: str) -> str:
    """Translate English msgid *message* via gettext. Must be :class:`str`."""
    if not isinstance(message, str):
        raise TypeError("gettext msgid must be str")

    global _translation
    if _translation is None:
        init_i18n()

    assert _translation is not None
    return _translation.gettext(message)
