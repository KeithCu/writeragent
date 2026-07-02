import datetime
import pytest
from unittest.mock import patch

from plugin.scripting.venv.venv_sandbox import convert_datetimes_and_deltas

def test_convert_datetimes_and_deltas_disabled():
    data = "2026-06-29"
    res = convert_datetimes_and_deltas(data, "en_US", convert_datetime=False)
    assert res == "2026-06-29"

def test_convert_datetimes_and_deltas_enabled():
    try:
        import pandas
        import dateparser
    except ImportError:
        pytest.skip("pandas or dateparser not installed in testing environment")

    # Test date-time strings
    res1 = convert_datetimes_and_deltas("2026-06-29", "en_US", convert_datetime=True)
    assert isinstance(res1, datetime.datetime)
    assert res1.year == 2026
    assert res1.month == 6
    assert res1.day == 29

    # Test locale-specific date-time strings (de_DE: DD.MM.YYYY)
    res2 = convert_datetimes_and_deltas("29.06.2026", "de_DE", convert_datetime=True)
    assert isinstance(res2, datetime.datetime)
    assert res2.year == 2026
    assert res2.month == 6
    assert res2.day == 29

    # Test timedelta strings
    res3 = convert_datetimes_and_deltas("3d 4h", "en_US", convert_datetime=True)
    assert isinstance(res3, datetime.timedelta)
    assert res3.days == 3
    assert res3.seconds == 4 * 3600

    # Test clock timedelta
    res4 = convert_datetimes_and_deltas("01:30:00", "en_US", convert_datetime=True)
    assert isinstance(res4, datetime.timedelta)
    assert res4.seconds == 5400  # 1 hour 30 mins

    # Test clock timedelta with localized characters (e.g. colon and characters)
    # The regex [^\W\d_]|: will match the colon and 'м' (Cyrillic letter)
    res_loc = convert_datetimes_and_deltas("01:30:00", "ru_RU", convert_datetime=True)
    assert isinstance(res_loc, datetime.timedelta)
    assert res_loc.seconds == 5400

    # Ensure a non-ASCII date-time (e.g. "29 июня 2026") parses correctly with the new regex
    # It has Cyrillic characters, which will match the unicode-aware letters regex
    res_ru_date = convert_datetimes_and_deltas("29 июня 2026", "ru_RU", convert_datetime=True)
    assert isinstance(res_ru_date, datetime.datetime)
    assert res_ru_date.year == 2026
    assert res_ru_date.month == 6
    assert res_ru_date.day == 29

    # Test nested lists
    data_list = [["2026-06-29", "12h"], ["not a date", 42]]
    res_list = convert_datetimes_and_deltas(data_list, "en_US", convert_datetime=True)
    assert isinstance(res_list[0][0], datetime.datetime)
    assert isinstance(res_list[0][1], datetime.timedelta)
    assert res_list[1][0] == "not a date"
    assert res_list[1][1] == 42

def test_convert_datetimes_missing_dependencies():
    # Force ImportError on dateparser/pandas
    with patch("builtins.__import__", side_effect=ImportError("mocked import error")):
        with pytest.raises(ImportError) as excinfo:
            convert_datetimes_and_deltas("2026-06-29", "en_US", convert_datetime=True)
        assert "requires both 'pandas' and 'dateparser'" in str(excinfo.value)
