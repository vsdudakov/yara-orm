"""Timezone helper functions."""

import datetime as dt

import pytest

from yara_orm import timezone


def test_defaults_and_parse():
    """
    GIVEN the default configuration
    WHEN the timezone accessors are read and a name is parsed
    THEN UTC is the default, use_tz is off, and parse_timezone resolves a zone
    """
    assert timezone.get_timezone() == "UTC"
    assert timezone.get_use_tz() is False
    assert timezone.get_default_timezone() == timezone.parse_timezone("UTC")


def test_is_aware_is_naive():
    """
    GIVEN aware and naive datetimes
    WHEN is_aware / is_naive inspect them
    THEN each reports the correct awareness
    """
    aware = dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)
    naive = dt.datetime(2021, 1, 1)
    assert timezone.is_aware(aware) and not timezone.is_naive(aware)
    assert timezone.is_naive(naive) and not timezone.is_aware(naive)


def test_make_aware_and_naive_roundtrip():
    """
    GIVEN a naive datetime
    WHEN it is made aware and then naive again
    THEN make_aware attaches a zone and make_naive strips it back
    """
    naive = dt.datetime(2021, 6, 1, 12, 0, 0)
    aware = timezone.make_aware(naive, "UTC")
    assert timezone.is_aware(aware)
    assert timezone.make_naive(aware, "UTC") == naive
    with pytest.raises(ValueError):
        timezone.make_aware(aware)  # already aware
    with pytest.raises(ValueError):
        timezone.make_naive(naive)  # already naive


def test_localtime_converts_zone():
    """
    GIVEN a UTC datetime
    WHEN localtime converts it to a +offset zone
    THEN the wall-clock shifts by the offset while the instant is preserved
    """
    utc = dt.datetime(2021, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    out = timezone.localtime(utc, "Asia/Kolkata")  # UTC+5:30
    assert out.hour == 17 and out.minute == 30
    assert out == utc

    # A naive input is made aware in the default zone first.
    naive = dt.datetime(2021, 6, 1, 12, 0, 0)
    assert timezone.is_aware(timezone.localtime(naive))
    # With no argument it localises the current time.
    assert timezone.is_aware(timezone.localtime())


def test_now_respects_use_tz():
    """
    GIVEN the configurable use_tz flag
    WHEN now() is called with it off and on
    THEN it returns a naive datetime when off and an aware one when on
    """
    assert timezone.is_naive(timezone.now())
    timezone._set_config(use_tz=True)
    try:
        assert timezone.is_aware(timezone.now())
        timezone._set_config(timezone="UTC")  # set the zone independently
        assert timezone.get_timezone() == "UTC"
    finally:
        timezone._set_config(use_tz=False)
