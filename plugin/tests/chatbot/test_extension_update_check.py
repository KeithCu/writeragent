# WriterAgent tests — extension update.xml parsing and version ordering

from plugin.chatbot.extension_update_check import (
    EXPECTED_EXTENSION_ID,
    parse_update_xml,
    remote_is_newer,
    version_tuple,
)

SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006"
             xmlns:d="http://openoffice.org/extensions/description/2006"
             xmlns:xlink="http://www.w3.org/1999/xlink">
    <identifier value="org.extension.writeragent" />
    <version value="0.7.1" />
    <update-download>
        <src xlink:href="https://github.com/KeithCu/writeragent/releases/latest/download/writeragent.oxt" />
    </update-download>
</description>
"""

WRONG_ID_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006">
    <identifier value="other.extension" />
    <version value="9.9.9" />
</description>
"""


def test_version_tuple_ordering():
    assert version_tuple("0.7.2") < version_tuple("0.7.10")
    assert version_tuple("0.7.10") > version_tuple("0.7.2")
    assert version_tuple("1.0.0") > version_tuple("0.9.9")


def test_version_tuple_invalid():
    assert version_tuple("") is None
    assert version_tuple("1.a.0") is None


def test_remote_is_newer():
    assert remote_is_newer("0.8.0", "0.7.9") is True
    assert remote_is_newer("0.7.1", "0.7.1") is False
    assert remote_is_newer("0.7.0", "0.7.1") is False


def test_parse_update_xml_sample():
    ident, ver = parse_update_xml(SAMPLE_XML)
    assert ident == EXPECTED_EXTENSION_ID
    assert ver == "0.7.1"


def test_parse_update_xml_wrong_identifier_still_parses():
    ident, ver = parse_update_xml(WRONG_ID_XML)
    assert ident == "other.extension"
    assert ver == "9.9.9"


def test_identifier_mismatch_means_ignore_for_update_signal():
    """Caller must reject when ident != EXPECTED_EXTENSION_ID."""
    ident, ver = parse_update_xml(WRONG_ID_XML)
    assert ident != EXPECTED_EXTENSION_ID
    # would not treat as update even though remote > local
    assert remote_is_newer(ver, "0.0.1") is True
