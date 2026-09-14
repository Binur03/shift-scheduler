"""Venue-local <-> UTC conversions.

Storage conventions (see models.py): shift schedules are naive venue-local
wall-clock values; every ``*_utc`` column is naive UTC. All conversion goes
through here so the rules live in one place.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models import DEFAULT_VENUE_TIMEZONE


def venue_zone(tz_name: str | None) -> ZoneInfo:
    """ZoneInfo for a venue, falling back to the default on bad/missing names."""
    try:
        return ZoneInfo(tz_name or DEFAULT_VENUE_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_VENUE_TIMEZONE)


def is_valid_timezone(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def local_to_utc_naive(local_dt: datetime, tz_name: str | None) -> datetime:
    """Interpret a naive venue-local datetime and return naive UTC.

    During the DST fall-back hour the earlier instant is used (fold=0), which
    is the conventional reading of an ambiguous schedule time.
    """
    aware = local_dt.replace(tzinfo=venue_zone(tz_name))
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def utc_naive_to_local(utc_dt: datetime | None, tz_name: str | None) -> datetime | None:
    """Convert a stored naive-UTC timestamp to an aware venue-local datetime."""
    if utc_dt is None:
        return None
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(venue_zone(tz_name))


def shift_window_utc(
    shift_date: date, start: time, end: time, tz_name: str | None
) -> tuple[datetime, datetime]:
    """Scheduled [start, end) of a shift as naive UTC.

    Overnight shifts (end <= start, e.g. 22:00-06:00) end on the next day.
    """
    start_local = datetime.combine(shift_date, start)
    end_local = datetime.combine(shift_date, end)
    if end_local <= start_local:
        end_local += timedelta(days=1)
    return (
        local_to_utc_naive(start_local, tz_name),
        local_to_utc_naive(end_local, tz_name),
    )


def format_local_clock(utc_dt: datetime | None, tz_name: str | None) -> str:
    """Short venue-local clock time for SMS replies, e.g. "8:02 AM"."""
    local = utc_naive_to_local(utc_dt, tz_name)
    if local is None:
        return ""
    return local.strftime("%I:%M %p").lstrip("0")
