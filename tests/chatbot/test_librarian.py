import sys
from typing import Any
from unittest.mock import MagicMock, patch
import pytest

from plugin.tests.testing_utils import setup_uno_mocks


class MockNamedValue:
    Name: str = ""
    Value: Any = None


@pytest.fixture(autouse=True)
def _uno_mocks():
    setup_uno_mocks()
    setattr(sys.modules["com.sun.star.beans"], "NamedValue", MockNamedValue)


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
