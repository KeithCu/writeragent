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
import sys
import gettext
import logging

from plugin.framework.uno_context import get_plugin_dir

log = logging.getLogger("writeragent.i18n")

_translation = None


def get_lo_locale(ctx=None):
    """Attempt to determine the LibreOffice UI locale."""
    try:
        import uno
        if ctx is None:
            ctx = uno.getComponentContext()
        smgr = ctx.getServiceManager()
        # Fallback to a simple locale lookup if no advanced config is found
        config_provider = smgr.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", ctx)
        ca = config_provider.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationAccess",
            (uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="nodepath", Value="/org.openoffice.Setup/L10N"),)
        )

        locale = ca.getPropertyValue("ooLocale")
        if locale:
            # LibreOffice often returns "en-US", gettext prefers "en_US"
            return locale.replace("-", "_")
    except Exception as e:
        log.debug("Failed to determine LibreOffice locale: %s", e)

    # Fallback to system environment variable
    return os.environ.get("LANG", "en_US").split(".")[0]


def init_i18n(ctx=None):
    """Initialize gettext translation based on LibreOffice locale."""
    global _translation

    if _translation is not None:
        return  # Already initialized

    try:
        locale = get_lo_locale(ctx)
        locales_dir = os.path.join(get_plugin_dir(), "locales")

        log.debug("Initializing i18n for locale: %s, locales_dir: %s", locale, locales_dir)

        _translation = gettext.translation(
            domain="writeragent",
            localedir=locales_dir,
            languages=[locale],
            fallback=True
        )
    except Exception as e:
        log.debug("Failed to initialize i18n: %s. Falling back to default gettext.", e)
        _translation = gettext.NullTranslations()


def _(message):
    """Translate a message string using the initialized gettext translation.

    If i18n is not initialized or the translation is missing, the original
    message is returned.
    """
    global _translation
    if _translation is None:
        init_i18n()

    # Ensure message is a string before passing to gettext
    if not isinstance(message, str):
        return str(message)

    return _translation.gettext(message)
