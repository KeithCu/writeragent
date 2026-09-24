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


