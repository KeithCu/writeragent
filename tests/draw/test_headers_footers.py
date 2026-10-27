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


class TestAccessorsAndCollector:
    def test_get_text(self):
        from unittest.mock import MagicMock
        from plugin.draw.headers_footers import _get_text

        assert _get_text(None) == ""
        assert _get_text(object()) == ""

        shape = MagicMock()
        shape.getString.return_value = "hello"
        assert _get_text(shape) == "hello"

        shape.getString.side_effect = RuntimeError("failed")
        assert _get_text(shape) == ""

        class DisposedException(Exception):
            pass

        shape.getString.side_effect = DisposedException("disposed")
        import pytest

        with pytest.raises(DisposedException):
            _get_text(shape)

    def test_get_bool_visible_without_fallback(self):
        from unittest.mock import MagicMock
        from plugin.draw.headers_footers import _get_bool

        assert _get_bool(None, "Visible", fallback_attr=False) is False

        # When getPropertyValue raises, fallback_attr=False must return False without checking getattr
        shape = MagicMock()
        shape.getPropertyValue.side_effect = RuntimeError("unsupported")
        shape.Visible = True
        assert _get_bool(shape, "Visible", fallback_attr=False) is False

        # When shape has no getPropertyValue, fallback to getattr
        plain = MagicMock(spec=["Visible"])
        plain.Visible = True
        assert _get_bool(plain, "Visible", fallback_attr=False) is True

    def test_get_bool_fixed_fallback(self):
        from unittest.mock import MagicMock
        from plugin.draw.headers_footers import _get_bool

        shape = MagicMock()
        shape.getPropertyValue.side_effect = RuntimeError("unsupported")
        shape.IsFixed = True
        # fallback_attr=True falls back to getattr on failed getPropertyValue
        assert _get_bool(shape, "IsFixed", fallback_attr=True) is True

    def test_shape_accessors_swallow_hasattr_exception(self):
        class ExplodingShape:
            def __getattr__(self, name):
                raise RuntimeError(f"PyUNO UnknownPropertyException for {name}")

        exploding = ExplodingShape()
        from plugin.draw.headers_footers import _get_text, _set_text, _set_visible, _get_bool

        assert _get_text(exploding) == ""
        assert _set_text(exploding, "hello") is False
        assert _set_visible(exploding, True) is False
        assert _get_bool(exploding, "Visible") is False

    def test_collect_hf_shapes_strict_beats_supported_names(self):
        from plugin.draw.headers_footers import _SVC_HEADER, _collect_hf_shapes

        class ShapeLoose:
            def getSupportedServiceNames(self):
                return ["com.sun.star.presentation.HeaderShape"]

        class ShapeStrict:
            ShapeType = "com.sun.star.presentation.HeaderShape"

        shape0 = ShapeLoose()
        shape1 = ShapeStrict()

        class FakePage:
            def getCount(self):
                return 2

            def getByIndex(self, i):
                return [shape0, shape1][i]

        res = _collect_hf_shapes(FakePage())
        assert res[_SVC_HEADER] is shape1

    def test_tool_parameter_property_names(self):
        from plugin.draw.headers_footers import GetHeadersFooters, SetHeadersFooters

        assert set(GetHeadersFooters.parameters["properties"].keys()) == {"page", "is_master_page"}
        assert set(SetHeadersFooters.parameters["properties"].keys()) == {
            "page",
            "is_master_page",
            "header",
            "footer",
            "date_time",
            "header_visible",
            "footer_visible",
            "page_number_visible",
            "date_time_visible",
            "date_time_fixed",
        }


class _FakeShape:
    def __init__(self, shape_type, props=None):
        self.ShapeType = shape_type
        self.text = ""
        self.props = dict(props or {})

    def getString(self):
        return self.text

    def setString(self, value):
        self.text = value

    def getPropertyValue(self, name):
        return self.props[name]

    def setPropertyValue(self, name, value):
        self.props[name] = value


class _FakePage:
    def __init__(self, shapes=()):
        self.shapes = list(shapes)
        self.props = {}

    def getCount(self):
        return len(self.shapes)

    def getByIndex(self, index):
        return self.shapes[index]

    def getPropertyValue(self, name):
        return self.props[name]

    def setPropertyValue(self, name, value):
        self.props[name] = value


class _FakePages:
    def __init__(self, slides=()):
        self.slides = list(slides)

    def getCount(self):
        return len(self.slides)

    def getByIndex(self, index):
        return self.slides[index]


class _FakeSlide:
    def __init__(self, master=None):
        self.MasterPage = master


class _FakeDoc:
    def supportsService(self, name):
        return name == "com.sun.star.presentation.PresentationDocument"


def _run_set(pages, **args):
    from types import SimpleNamespace
    from unittest.mock import patch
    from plugin.draw.headers_footers import SetHeadersFooters

    ctx = SimpleNamespace(doc=_FakeDoc())
    with patch("plugin.draw.headers_footers.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value = pages
        return SetHeadersFooters().execute(ctx, **args)


def test_field_table_matches_set_schema():
    from plugin.draw.headers_footers import _FIELDS, SetHeadersFooters

    assert {f[0] for f in _FIELDS} == set(SetHeadersFooters.parameters["properties"]) - {"page", "is_master_page"}


def test_set_slide_footer_text_implies_visible():
    page = _FakePage()
    res = _run_set(_FakePages([page]), page=0, footer="X")
    assert res["status"] == "ok"
    assert res["updated_properties"] == 2
    assert page.props == {"FooterText": "X", "IsFooterVisible": True}


def test_set_slide_explicit_false_wins():
    page = _FakePage()
    res = _run_set(_FakePages([page]), page=0, footer="X", footer_visible=False)
    assert res["status"] == "ok"
    assert page.props["FooterText"] == "X"
    assert page.props["IsFooterVisible"] is False


def test_set_slide_date_time_implies_fixed_and_visible():
    page = _FakePage()
    res = _run_set(_FakePages([page]), page=0, date_time="2024-01-01")
    assert res["status"] == "ok"
    assert res["updated_properties"] == 3
    assert page.props["DateTimeText"] == "2024-01-01"
    assert page.props["IsDateTimeVisible"] is True
    assert page.props["IsDateTimeFixed"] is True


def test_set_slide_rejects_header():
    page = _FakePage()
    res = _run_set(_FakePages([page]), page=0, header="H")
    assert res["status"] == "error"
    assert "header" in res["message"]
    assert page.props == {}


def test_set_slide_write_failure_names_key():
    class ExplodingPage(_FakePage):
        def setPropertyValue(self, name, value):
            if name == "FooterText":
                raise RuntimeError("write failed")
            super().setPropertyValue(name, value)

    page = ExplodingPage()
    res = _run_set(_FakePages([page]), page=0, footer="X")
    assert res["status"] == "error"
    assert "footer" in res["message"]
    assert res.get("updated_properties", res.get("details", {}).get("updated_properties")) == 1


def test_set_master_footer_text_and_visibility():
    master = _FakePage([_FakeShape("com.sun.star.presentation.FooterShape")])
    res = _run_set(_FakePages([_FakeSlide(master)]), page=0, is_master_page=True, footer="Master Footer")
    assert res["status"] == "ok"
    assert res["updated_properties"] == 2
    shape = master.getByIndex(0)
    assert shape.text == "Master Footer"
    assert shape.props["Visible"] is True


def test_set_master_missing_shape_reports_keys():
    master = _FakePage([])
    res = _run_set(_FakePages([_FakeSlide(master)]), page=0, is_master_page=True, footer="Master Footer")
    assert res["status"] == "error"
    assert "footer" in res["message"]
    assert res.get("updated_properties", res.get("details", {}).get("updated_properties")) == 0


def test_set_master_rejects_date_time_fixed():
    master = _FakePage([_FakeShape("com.sun.star.presentation.FooterShape")])
    res = _run_set(_FakePages([_FakeSlide(master)]), page=0, is_master_page=True, footer="Footer", date_time_fixed=True)
    assert res["status"] == "error"
    assert "date_time_fixed" in res["message"]
    shape = master.getByIndex(0)
    assert shape.text == ""


def test_get_page_empty_deck_message():
    import pytest
    from types import SimpleNamespace
    from unittest.mock import patch
    from plugin.draw.headers_footers import _get_page
    from plugin.framework.errors import WriterAgentException

    ctx = SimpleNamespace(doc=_FakeDoc())
    with patch("plugin.draw.headers_footers.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value = _FakePages([])
        with pytest.raises(WriterAgentException) as exc_info:
            _get_page(ctx, 0, is_master_page=False)
        assert exc_info.value.code == "INVALID_INDEX"
        assert "no slides" in str(exc_info.value)


def test_set_text_ordinary_failure_returns_false():
    from plugin.draw.headers_footers import _set_text

    class Shape:
        def setString(self, _text):
            raise RuntimeError("write failed")

    assert _set_text(Shape(), "hello") is False
    assert _set_text(object(), "hello") is False


def test_set_text_disposed_reraises():
    import pytest
    from plugin.draw.headers_footers import _set_text

    class DisposedException(Exception):
        pass

    class Shape:
        def setString(self, _text):
            raise DisposedException("disposed")

    class LookupDisposed:
        @property
        def setString(self):
            raise DisposedException("disposed")

    with pytest.raises(DisposedException):
        _set_text(Shape(), "hello")
    with pytest.raises(DisposedException):
        _set_text(LookupDisposed(), "hello")



