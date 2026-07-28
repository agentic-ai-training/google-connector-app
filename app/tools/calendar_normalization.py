"""Deterministic Calendar timezone and date-time normalization."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TIMEZONE_ALIASES = {
    "utc": "UTC",
    "gmt": "UTC",
    "ist": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "indian": "Asia/Kolkata",
    "indian standard time": "Asia/Kolkata",
    "est": "America/New_York",
    "edt": "America/New_York",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "cet": "Europe/Paris",
}


def normalize_timezone(value: str | None) -> str:
    candidate = str(value or "").strip()
    canonical = TIMEZONE_ALIASES.get(candidate.casefold(), candidate)
    try:
        ZoneInfo(canonical)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            "Calendar timezone must be a valid IANA timezone such as "
            "Asia/Kolkata, Europe/London, or America/New_York"
        ) from exc
    return canonical


def _without_timezone_words(value: str, timezone_name: str) -> str:
    cleaned = value
    candidates = {
        timezone_name,
        timezone_name.replace("_", " "),
        *TIMEZONE_ALIASES,
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        cleaned = re.sub(
            rf"(?<![\w/]){re.escape(candidate)}(?![\w/])",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_calendar_datetime(
    value: str, timezone_name: str, *, now: datetime | None = None,
) -> str:
    """Return an offset-bearing RFC3339 date-time in ``timezone_name``."""
    timezone = ZoneInfo(normalize_timezone(timezone_name))
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Calendar date and time cannot be blank")
    iso_candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        else:
            parsed = parsed.astimezone(timezone)
        return parsed.isoformat()

    cleaned = _without_timezone_words(raw, timezone_name)
    relative = re.search(r"\b(today|tomorrow)\b", cleaned, re.IGNORECASE)
    time_match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        cleaned,
        re.IGNORECASE,
    )
    if not relative or not time_match:
        raise ValueError(
            "Calendar date-time must be RFC3339 or use a supported form such "
            "as 'tomorrow 10:00 AM'"
        )
    reference = (now or datetime.now(timezone)).astimezone(timezone)
    day = reference.date() + timedelta(
        days=1 if relative.group(1).casefold() == "tomorrow" else 0
    )
    hour = int(time_match.group(1)) % 12
    if time_match.group(3).casefold() == "pm":
        hour += 12
    minute = int(time_match.group(2) or 0)
    return datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=timezone,
    ).isoformat()


def normalize_calendar_window(
    start_datetime: str, end_datetime: str, timezone_name: str | None,
) -> tuple[str, str, str]:
    timezone = normalize_timezone(timezone_name)
    start = normalize_calendar_datetime(start_datetime, timezone)
    end = normalize_calendar_datetime(end_datetime, timezone)
    if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
        raise ValueError("Calendar end time must be after the start time")
    return start, end, timezone
