from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.librarian import (
    get_libreoffice_user_display_name,
    get_os_login_name,
    get_suggested_user_name,
)


def test_get_libreoffice_user_display_name_givenname():
    ctx = MagicMock(spec=["getServiceManager"])
    smgr = MagicMock()
    provider = MagicMock()
    access = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = provider
    provider.createInstanceWithArguments.return_value = access
    access.getPropertyValue.side_effect = lambda prop: "Alice" if prop == "givenname" else ""

    assert get_libreoffice_user_display_name(ctx) == "Alice"


def test_get_libreoffice_user_display_name_sn_fallback():
    ctx = MagicMock(spec=["getServiceManager"])
    smgr = MagicMock()
    provider = MagicMock()
    access = MagicMock()
    ctx.getServiceManager.return_value = smgr
    smgr.createInstanceWithContext.return_value = provider
    provider.createInstanceWithArguments.return_value = access
    access.getPropertyValue.side_effect = lambda prop: "" if prop == "givenname" else "Smith"

    assert get_libreoffice_user_display_name(ctx) == "Smith"


def test_get_libreoffice_user_display_name_failure_returns_none():
    ctx = MagicMock(spec=["getServiceManager"])
    ctx.getServiceManager.side_effect = RuntimeError("UNO error")
    assert get_libreoffice_user_display_name(ctx) is None


def test_get_os_login_name_success():
    with patch("getpass.getuser", return_value="alice"):
        assert get_os_login_name() == "alice"


def test_get_os_login_name_generic_filtered():
    with patch("getpass.getuser", return_value="root"):
        assert get_os_login_name() is None


def test_get_os_login_name_error_returns_none():
    with patch("getpass.getuser", side_effect=OSError("no user")):
        assert get_os_login_name() is None


def test_get_suggested_user_name_prefers_lo():
    with patch("plugin.chatbot.librarian.get_libreoffice_user_display_name", return_value="Alice"), \
         patch("plugin.chatbot.librarian.get_os_login_name", return_value="bob"):
        assert get_suggested_user_name(MagicMock()) == "Alice"


def test_get_suggested_user_name_falls_back_to_os():
    with patch("plugin.chatbot.librarian.get_libreoffice_user_display_name", return_value=None), \
         patch("plugin.chatbot.librarian.get_os_login_name", return_value="bob"):
        assert get_suggested_user_name(MagicMock()) == "bob"


def test_get_os_login_name_filters_generic_user():
    with patch("plugin.chatbot.librarian.getpass.getuser", return_value="user"):
        assert get_os_login_name() is None


def test_get_os_login_name_returns_normalized_login():
    with patch("plugin.chatbot.librarian.getpass.getuser", return_value="  Keith  "):
        assert get_os_login_name() == "Keith"


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param("Alice", "Smith", "Alice", id="test_get_libreoffice_user_display_name_prefers_givenname"),
        pytest.param("", "Jones", "Jones", id="test_get_libreoffice_user_display_name_falls_back_to_sn"),
    ],
)
def test_get_libreoffice_user_display_name_prefers_givenname(value, value_2, expected):
    access = MagicMock()
    access.getPropertyValue.side_effect = lambda key: {"givenname": value, "sn": value_2}[key]
    provider = MagicMock()
    provider.createInstanceWithArguments.return_value = access
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = provider
    ctx = MagicMock()
    ctx.ctx = ctx
    ctx.getServiceManager.return_value = smgr

    with patch.dict("sys.modules", {"com.sun.star.beans": MagicMock()}):
        assert get_libreoffice_user_display_name(ctx) == expected


@pytest.mark.parametrize(
    "return_value, expected",
    [
        pytest.param("Alice", "Alice", id="test_get_suggested_user_name_prefers_libreoffice_over_os"),
        pytest.param(None, "keithcu", id="test_get_suggested_user_name_falls_back_to_os_login"),
    ],
)
def test_get_suggested_user_name_prefers_libreoffice_over_os(return_value, expected):
    ctx = MagicMock()
    with patch("plugin.chatbot.librarian.get_libreoffice_user_display_name", return_value=return_value):
        with patch("plugin.chatbot.librarian.get_os_login_name", return_value="keithcu"):
            assert get_suggested_user_name(ctx) == expected
