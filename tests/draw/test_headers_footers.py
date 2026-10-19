# WriterAgent — unit tests for Impress header/footer tool arg handling


from plugin.draw.headers_footers import _coerce_bool_arg


class TestCoerceBoolArg:
    def test_bool_passthrough(self):
        assert (_coerce_bool_arg({"is_master_page": True}, "is_master_page")) is (True)
        assert (_coerce_bool_arg({"is_master_page": False}, "is_master_page")) is (False)

    def test_string_json(self):
        assert (_coerce_bool_arg({"is_master_page": "true"}, "is_master_page"))
        assert (_coerce_bool_arg({"is_master_page": "1"}, "is_master_page"))
        assert not (_coerce_bool_arg({"is_master_page": "false"}, "is_master_page"))
        assert not (_coerce_bool_arg({"is_master_page": ""}, "is_master_page"))

    def test_missing_defaults_false(self):
        assert not (_coerce_bool_arg({}, "is_master_page"))


class TestGetPage:
    def test_index_out_of_bounds_below_zero(self):
        from unittest.mock import MagicMock, patch
        import pytest
        from plugin.draw.headers_footers import _get_page
        from plugin.framework.errors import format_error_payload, WriterAgentException

        ctx = MagicMock()
        mock_pages = MagicMock()
        mock_pages.getCount.return_value = 3
        with patch("plugin.draw.headers_footers.DrawBridge") as mock_bridge_cls:
            mock_bridge_cls.return_value.get_pages.return_value = mock_pages
            with pytest.raises(WriterAgentException) as exc_info:
                _get_page(ctx, -1, is_master_page=False)
            payload = format_error_payload(exc_info.value)
            assert payload["code"] == "INVALID_INDEX"
            assert "out of bounds" in payload["message"]
            assert "2" in payload["message"]

    def test_index_out_of_bounds_at_or_above_count(self):
        from unittest.mock import MagicMock, patch
        import pytest
        from plugin.draw.headers_footers import _get_page
        from plugin.framework.errors import format_error_payload, WriterAgentException

        ctx = MagicMock()
        mock_pages = MagicMock()
        mock_pages.getCount.return_value = 3
        with patch("plugin.draw.headers_footers.DrawBridge") as mock_bridge_cls:
            mock_bridge_cls.return_value.get_pages.return_value = mock_pages
            with pytest.raises(WriterAgentException) as exc_info:
                _get_page(ctx, 3, is_master_page=False)
            payload = format_error_payload(exc_info.value)
            assert payload["code"] == "INVALID_INDEX"
            assert "out of bounds" in payload["message"]
            assert "2" in payload["message"]

    def test_master_page_is_none(self):
        from unittest.mock import MagicMock, patch
        import pytest
        from plugin.draw.headers_footers import _get_page
        from plugin.framework.errors import format_error_payload, WriterAgentException

        ctx = MagicMock()
        mock_pages = MagicMock()
        mock_pages.getCount.return_value = 3
        mock_slide = MagicMock()
        mock_slide.MasterPage = None
        mock_pages.getByIndex.return_value = mock_slide
        with patch("plugin.draw.headers_footers.DrawBridge") as mock_bridge_cls:
            mock_bridge_cls.return_value.get_pages.return_value = mock_pages
            with pytest.raises(WriterAgentException) as exc_info:
                _get_page(ctx, 0, is_master_page=True)
            payload = format_error_payload(exc_info.value)
            assert payload["code"] == "NO_MASTER"
            assert "no master page" in payload["message"].lower()

    def test_master_page_access_raises_non_disposal_exception(self):
        from unittest.mock import MagicMock, patch, PropertyMock
        import pytest
        from plugin.draw.headers_footers import _get_page
        from plugin.framework.errors import format_error_payload, WriterAgentException

        ctx = MagicMock()
        mock_pages = MagicMock()
        mock_pages.getCount.return_value = 3
        cause = RuntimeError("lookup failed")
        mock_slide = MagicMock()
        type(mock_slide).MasterPage = PropertyMock(side_effect=cause)
        mock_pages.getByIndex.return_value = mock_slide
        with patch("plugin.draw.headers_footers.DrawBridge") as mock_bridge_cls:
            mock_bridge_cls.return_value.get_pages.return_value = mock_pages
            with pytest.raises(WriterAgentException) as exc_info:
                _get_page(ctx, 0, is_master_page=True)
            payload = format_error_payload(exc_info.value)
            assert payload["code"] == "NO_MASTER"
            assert exc_info.value.__cause__ is cause

    def test_master_page_access_raises_disposed_exception(self):
        from unittest.mock import MagicMock, patch, PropertyMock
        import pytest
        from plugin.draw.headers_footers import _get_page
        from plugin.framework.errors import WriterAgentException

        class DisposedException(Exception):
            pass

        ctx = MagicMock()
        mock_pages = MagicMock()
        mock_pages.getCount.return_value = 3
        mock_slide = MagicMock()
        type(mock_slide).MasterPage = PropertyMock(side_effect=DisposedException("page disposed"))
        mock_pages.getByIndex.return_value = mock_slide
        with patch("plugin.draw.headers_footers.DrawBridge") as mock_bridge_cls:
            mock_bridge_cls.return_value.get_pages.return_value = mock_pages
            with pytest.raises(DisposedException) as exc_info:
                _get_page(ctx, 0, is_master_page=True)
            assert not isinstance(exc_info.value, WriterAgentException)


