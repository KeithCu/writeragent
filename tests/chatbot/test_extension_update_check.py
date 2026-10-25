# WriterAgent tests — extension update.xml parsing and version ordering

import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.extension_update_check import (
    _FAILURE_RETRY_SECONDS,
    UPDATE_CHECK_PROFILES,
    get_update_check_profile,
    parse_update_xml,
    reset_extension_update_check_schedule_for_tests,
    run_extension_update_check,
    schedule_extension_update_check_once,
    version_tuple,
)
from plugin.framework.constants import (
    EXTENSION_ID_LIBREHARPER,
    EXTENSION_ID_LIBREPY,
    EXTENSION_ID_WRITERAGENT,
)

_WRITERAGENT_PROFILE = UPDATE_CHECK_PROFILES[EXTENSION_ID_WRITERAGENT]

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

LIBREPY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006"
             xmlns:xlink="http://www.w3.org/1999/xlink">
    <identifier value="org.extension.librepy" />
    <version value="0.8.0" />
    <update-download>
        <src xlink:href="https://github.com/KeithCu/writeragent/releases/latest/download/LibrePy.oxt" />
    </update-download>
</description>
"""

LIBREHARPER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006"
             xmlns:xlink="http://www.w3.org/1999/xlink">
    <identifier value="org.extension.libreharper" />
    <version value="0.8.1" />
    <update-download>
        <src xlink:href="https://github.com/KeithCu/writeragent/releases/latest/download/LibreHarper.oxt" />
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


def test_parse_update_xml_sample():
    ident, ver = parse_update_xml(SAMPLE_XML)
    assert ident == _WRITERAGENT_PROFILE.extension_id
    assert ver == "0.7.1"


def test_parse_update_xml_librepy():
    ident, ver = parse_update_xml(LIBREPY_XML)
    assert ident == EXTENSION_ID_LIBREPY
    assert ver == "0.8.0"


def test_parse_update_xml_libreharper():
    ident, ver = parse_update_xml(LIBREHARPER_XML)
    assert ident == EXTENSION_ID_LIBREHARPER
    assert ver == "0.8.1"


def test_parse_update_xml_wrong_identifier_still_parses():
    ident, ver = parse_update_xml(WRONG_ID_XML)
    assert ident == "other.extension"
    assert ver == "9.9.9"


def test_identifier_mismatch_means_ignore_for_update_signal():
    """Caller must reject when ident != expected profile extension id."""
    ident, ver = parse_update_xml(WRONG_ID_XML)
    assert ident != _WRITERAGENT_PROFILE.extension_id
    # would not treat as update even though remote > local
    assert version_tuple(ver or "") > version_tuple("0.0.1")


def test_update_check_profiles_cover_three_products():
    assert set(UPDATE_CHECK_PROFILES) == {
        EXTENSION_ID_WRITERAGENT,
        EXTENSION_ID_LIBREPY,
        EXTENSION_ID_LIBREHARPER,
    }
    wa = get_update_check_profile(EXTENSION_ID_WRITERAGENT)
    lp = get_update_check_profile(EXTENSION_ID_LIBREPY)
    lh = get_update_check_profile(EXTENSION_ID_LIBREHARPER)
    assert wa is not None and wa.config_key_epoch == "extension_update_check_epoch"
    assert lp is not None and lp.config_key_epoch == "librepy_update_check_epoch"
    assert lh is not None and lh.config_key_epoch == "libreharper_update_check_epoch"
    # Dual-install: WriterAgent and LibreHarper must not share a cadence key.
    assert wa.config_key_epoch != lh.config_key_epoch
    assert get_update_check_profile("other.extension") is None


def test_schedule_once_allows_two_products_same_process():
    reset_extension_update_check_schedule_for_tests()
    ctx = MagicMock()
    with patch("plugin.framework.worker_pool.run_in_background") as run_bg:
        schedule_extension_update_check_once(ctx, EXTENSION_ID_WRITERAGENT)
        schedule_extension_update_check_once(ctx, EXTENSION_ID_LIBREHARPER)
        # Second call for same product is a no-op.
        schedule_extension_update_check_once(ctx, EXTENSION_ID_WRITERAGENT)
        assert run_bg.call_count == 2
        kw_ids = {c.kwargs["extension_id"] for c in run_bg.call_args_list}
        assert kw_ids == {EXTENSION_ID_WRITERAGENT, EXTENSION_ID_LIBREHARPER}
        assert all(c.kwargs.get("dedicated") is True for c in run_bg.call_args_list)
    reset_extension_update_check_schedule_for_tests()


def test_schedule_once_skips_unknown_product():
    reset_extension_update_check_schedule_for_tests()
    with patch("plugin.framework.worker_pool.run_in_background") as run_bg:
        schedule_extension_update_check_once(MagicMock(), "org.extension.unknown")
        run_bg.assert_not_called()
    reset_extension_update_check_schedule_for_tests()


def test_run_extension_update_check_dialog_formatting(monkeypatch):
    # QueueExecutor.post inlines under WRITERAGENT_TESTING; otherwise MagicMock uno
    # "succeeds" at AsyncCallback and the dialog callback never drains.
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    ctx = MagicMock()
    with patch("plugin.framework.client.requests.sync_request", return_value=SAMPLE_XML) as mock_sync, \
         patch("plugin.chatbot.dialogs.msgbox") as mock_msgbox, \
         patch("plugin.framework.config.get_config_float", return_value=0.0), \
         patch("plugin.framework.config.set_config") as mock_set_config, \
         patch("plugin.version.EXTENSION_VERSION", "0.7.0"):
        run_extension_update_check(ctx, EXTENSION_ID_WRITERAGENT)

        assert mock_sync.call_args.kwargs["max_bytes"] == 64 * 1024
        assert mock_msgbox.call_count == 1
        msg_args = mock_msgbox.call_args[0]
        assert msg_args[1] == "Update available"
        assert "A newer WriterAgent (0.7.1) is available." in msg_args[2]
        mock_set_config.assert_called_once()
        assert mock_set_config.call_args[0][0] == _WRITERAGENT_PROFILE.config_key_epoch
        assert mock_set_config.call_args[0][1] == pytest.approx(time.time(), abs=60)


def test_transient_fetch_failure_retries_in_six_hours(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    ctx = MagicMock()
    with patch("plugin.framework.client.requests.sync_request", side_effect=OSError("offline")), \
         patch("plugin.framework.config.get_config_float", return_value=0.0), \
         patch("plugin.framework.config.set_config") as mock_set_config:
        run_extension_update_check(ctx, EXTENSION_ID_WRITERAGENT)

        mock_set_config.assert_called_once_with(
            _WRITERAGENT_PROFILE.config_key_epoch,
            pytest.approx(time.time() - 7 * 24 * 3600 + _FAILURE_RETRY_SECONDS, abs=60),
        )


def test_not_due_check_writes_nothing(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    ctx = MagicMock()
    with patch("plugin.framework.config.get_config_float", return_value=time.time()), \
         patch("plugin.framework.client.requests.sync_request") as mock_sync, \
         patch("plugin.framework.config.set_config") as mock_set_config:
        run_extension_update_check(ctx, EXTENSION_ID_WRITERAGENT)
        mock_sync.assert_not_called()
        mock_set_config.assert_not_called()


def test_parse_update_xml_ignores_nested_metadata():
    data = b"""<?xml version="1.0" encoding="UTF-8"?>
<description xmlns="http://openoffice.org/extensions/description/2006"
             xmlns:d="http://openoffice.org/extensions/description/2006"
             xmlns:xlink="http://www.w3.org/1999/xlink">
    <identifier value="org.extension.writeragent" />
    <version value="0.7.1" />
    <update-download>
        <identifier value="evil" />
        <version value="9.9.9" />
        <src xlink:href="https://example.com/bad.oxt" />
    </update-download>
</description>
"""
    ident, ver = parse_update_xml(data)
    assert ident == "org.extension.writeragent"
    assert ver == "0.7.1"


