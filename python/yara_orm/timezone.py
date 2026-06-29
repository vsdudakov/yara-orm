"""Timezone helpers.

A small wrapper over the stdlib ``datetime`` / ``zoneinfo`` so callers can make
values aware/naive and read the configured default timezone. The default
timezone (``"UTC"``) and the ``use_tz`` flag can be set via :func:`_set_config`
(wired from ``YaraOrm.init``).
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

_TIMEZONE = "UTC"
_USE_TZ = False


def _set_config(timezone: str | None = None, use_tz: bool | None = None) -> None:
    """Set the process-wide default timezone and ``use_tz`` flag.

    Args:
        timezone: IANA timezone name, or None to leave unchanged.
        use_tz: Whether :func:`now` returns aware datetimes, or None to leave it.

    Returns:
        None
    """
    global _TIMEZONE, _USE_TZ
    if timezone is not None:
        _TIMEZONE = timezone
    if use_tz is not None:
        _USE_TZ = use_tz


def get_timezone() -> str:
    """Return the configured default timezone name.

    Returns:
        The IANA timezone name (defaults to ``"UTC"``).
    """
    return _TIMEZONE


def get_use_tz() -> bool:
    """Report whether timezone-aware datetimes are in use.

    Returns:
        ``True`` when ``use_tz`` is enabled.
    """
    return _USE_TZ


def get_default_timezone() -> _dt.tzinfo:
    """Return the configured default timezone as a ``tzinfo``.

    Returns:
        The default timezone object.
    """
    return ZoneInfo(_TIMEZONE)


def parse_timezone(timezone: str) -> _dt.tzinfo:
    """Resolve an IANA timezone name to a ``tzinfo``.

    Args:
        timezone: The IANA timezone name.

    Returns:
        The corresponding timezone object.
    """
    return ZoneInfo(timezone)


def is_aware(value: _dt.datetime) -> bool:
    """Report whether a datetime is timezone-aware.

    Args:
        value: The datetime to inspect.

    Returns:
        ``True`` if ``value`` carries timezone information.
    """
    return value.utcoffset() is not None


def is_naive(value: _dt.datetime) -> bool:
    """Report whether a datetime is naive (no timezone).

    Args:
        value: The datetime to inspect.

    Returns:
        ``True`` if ``value`` has no timezone information.
    """
    return value.utcoffset() is None


def now() -> _dt.datetime:
    """Return the current time, aware when ``use_tz`` is enabled.

    Returns:
        An aware datetime in the default timezone, or a naive UTC datetime.
    """
    if _USE_TZ:
        return _dt.datetime.now(get_default_timezone())
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def make_aware(value: _dt.datetime, timezone: str | None = None) -> _dt.datetime:
    """Attach a timezone to a naive datetime.

    Args:
        value: A naive datetime.
        timezone: IANA timezone name, or None for the default.

    Returns:
        The value localised to the timezone.

    Raises:
        ValueError: If ``value`` is already aware.
    """
    if is_aware(value):
        raise ValueError("make_aware expects a naive datetime")
    tz = parse_timezone(timezone) if timezone else get_default_timezone()
    return value.replace(tzinfo=tz)


def make_naive(value: _dt.datetime, timezone: str | None = None) -> _dt.datetime:
    """Strip the timezone from an aware datetime, converting first.

    Args:
        value: An aware datetime.
        timezone: IANA timezone to convert to before stripping, or the default.

    Returns:
        The naive datetime in the target timezone.

    Raises:
        ValueError: If ``value`` is naive.
    """
    if is_naive(value):
        raise ValueError("make_naive expects an aware datetime")
    tz = parse_timezone(timezone) if timezone else get_default_timezone()
    return value.astimezone(tz).replace(tzinfo=None)


def localtime(value: _dt.datetime | None = None, timezone: str | None = None) -> _dt.datetime:
    """Convert an aware datetime (or now) into the given timezone.

    Args:
        value: An aware datetime, or None to use :func:`now`.
        timezone: IANA timezone name, or None for the default.

    Returns:
        The value expressed in the target timezone.
    """
    if value is None:
        value = now()
    if is_naive(value):
        # Naive datetimes in this ORM represent UTC (that is what ``now()``
        # returns when ``use_tz`` is off), so interpret them as UTC rather than
        # mislabelling the wall-clock as the (possibly non-UTC) default zone.
        value = value.replace(tzinfo=_dt.timezone.utc)
    tz = parse_timezone(timezone) if timezone else get_default_timezone()
    return value.astimezone(tz)


__all__ = [
    "get_timezone",
    "get_use_tz",
    "get_default_timezone",
    "parse_timezone",
    "is_aware",
    "is_naive",
    "now",
    "make_aware",
    "make_naive",
    "localtime",
]
