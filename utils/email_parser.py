"""Regex parser that turns a vendor's shift-request email into shift lines.

Accepted line formats (one shift per line; case-insensitive):

  Pipe-delimited
    09/20/2026 | 4:00 PM - 11:00 PM | Concessions Stand 12 | 6
    2026-09-20 | 16:00-23:00 | Suite Level Bartender | Qty: 4

  Labeled
    Date: 09/20/2026  Time: 4pm to 11pm  Position: Concessions Stand 12  Qty: 6

Rules:
  * Dates: YYYY-MM-DD or M/D/YYYY (two-digit years are 20xx).
  * Times: with AM/PM ("4pm", "4:00 PM", "4 p.m.") or 24-hour "HH:MM".
    A bare "4" with no AM/PM is rejected as ambiguous.
  * An end time at or before the start time means the shift runs overnight.
  * The position must match a Job title (matched later, at ingestion).
  * Headcount 1-500.

Lines that contain both a date and a time range but don't fit a format are
reported as errors (so the admin sees what was missed). Everything else —
greetings, signatures, quoted replies (lines starting with ">") — is ignored.

TODO: validate these patterns against real vendor emails; extend the format
list per vendor rather than loosening the regexes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta

MAX_HEADCOUNT = 500

_DATE = r"(?P<date>\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))"
_TIME = r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?\s?m\.?)?"
_RANGE = rf"(?P<start>{_TIME})\s*(?:-|–|—|\bto\b)\s*(?P<end>{_TIME})"
_SEP = r"\s*[,;|]?\s*"

_PIPE_LINE = re.compile(
    rf"^\s*{_DATE}\s*\|\s*{_RANGE}\s*\|\s*(?P<position>[^|]+?)\s*\|\s*"
    rf"(?:(?:qty|quantity|x)\s*:?\s*)?(?P<count>\d{{1,3}})"
    rf"\s*(?:staff|workers|people)?\s*$",
    re.IGNORECASE,
)
_LABELED_LINE = re.compile(
    rf"^\s*date\s*:\s*{_DATE}{_SEP}time\s*:\s*{_RANGE}{_SEP}"
    rf"(?:position|role|job|stand|location)\s*:\s*(?P<position>.+?){_SEP}"
    rf"(?:qty|quantity|headcount|staff|need(?:ed)?)\s*:\s*(?P<count>\d{{1,3}})\s*$",
    re.IGNORECASE,
)

# Heuristics for "this line was meant to be a shift".
_DATE_HINT = re.compile(r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_RANGE_HINT = re.compile(rf"{_TIME}\s*(?:-|–|—|\bto\b)\s*{_TIME}", re.IGNORECASE)

_TIME_PARTS = re.compile(
    r"^(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?:(?P<ap>[ap])\.?\s?m\.?)?$",
    re.IGNORECASE,
)

FORMAT_HINT = (
    "expected e.g. '09/20/2026 | 4:00 PM - 11:00 PM | Position | 6' or "
    "'Date: 09/20/2026 Time: 4pm-11pm Position: X Qty: 6'"
)


@dataclass(frozen=True)
class ParsedShiftLine:
    line_no: int
    text: str
    work_date: date
    start: time
    end: time
    position: str
    headcount: int


@dataclass(frozen=True)
class LineError:
    line_no: int
    text: str
    error: str

    def as_dict(self) -> dict:
        return {"line": self.line_no, "text": self.text, "error": self.error}


@dataclass
class ParseResult:
    shifts: list[ParsedShiftLine] = field(default_factory=list)
    errors: list[LineError] = field(default_factory=list)


def parse_time(raw: str) -> time:
    """Parse "4pm", "4:30 PM", "4 p.m." or 24-hour "16:30"."""
    match = _TIME_PARTS.match(raw.strip())
    if not match:
        raise ValueError(f"unreadable time '{raw.strip()}'")
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    meridiem = (match.group("ap") or "").lower()
    if minute > 59:
        raise ValueError(f"invalid minutes in '{raw.strip()}'")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"invalid 12-hour time '{raw.strip()}'")
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    else:
        if match.group("m") is None:
            raise ValueError(
                f"ambiguous time '{raw.strip()}' — add AM/PM or use 24-hour HH:MM"
            )
        if hour > 23:
            raise ValueError(f"invalid 24-hour time '{raw.strip()}'")
    return time(hour, minute)


def parse_date(raw: str) -> date:
    raw = raw.strip()
    if "-" in raw:
        year, month, day = (int(p) for p in raw.split("-"))
    else:
        month, day, year = (int(p) for p in raw.split("/"))
        if year < 100:
            year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        # Python's own wording varies by version; keep the admin-facing text stable.
        raise ValueError(f"'{raw}' is not a real calendar date") from None


def _build_line(match: re.Match, line_no: int, text: str, today: date) -> ParsedShiftLine:
    work_date = parse_date(match.group("date"))
    if work_date < today - timedelta(days=1):
        raise ValueError(f"date {work_date:%m/%d/%Y} is in the past")
    if work_date > today + timedelta(days=366):
        raise ValueError(f"date {work_date:%m/%d/%Y} is more than a year out")

    start = parse_time(match.group("start"))
    end = parse_time(match.group("end"))
    if start == end:
        raise ValueError("start and end time are the same")

    position = re.sub(r"\s+", " ", match.group("position")).strip()
    if not position or len(position) > 160:
        raise ValueError("position is missing or too long")

    headcount = int(match.group("count"))
    if not 1 <= headcount <= MAX_HEADCOUNT:
        raise ValueError(f"headcount must be 1-{MAX_HEADCOUNT}")

    return ParsedShiftLine(line_no, text, work_date, start, end, position, headcount)


def parse_shift_email(body: str, *, today: date) -> ParseResult:
    """Extract shift lines from a plain-text email body."""
    result = ParseResult()
    seen: set[tuple] = set()

    for line_no, raw_line in enumerate((body or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(">"):
            continue

        match = _PIPE_LINE.match(line) or _LABELED_LINE.match(line)
        if match is None:
            if _DATE_HINT.search(line) and _RANGE_HINT.search(line):
                result.errors.append(
                    LineError(line_no, line[:300], f"couldn't read this shift line; {FORMAT_HINT}")
                )
            continue

        try:
            parsed = _build_line(match, line_no, line[:300], today)
        except ValueError as exc:
            result.errors.append(LineError(line_no, line[:300], str(exc)))
            continue

        key = (parsed.work_date, parsed.start, parsed.end, parsed.position.lower())
        if key in seen:
            result.errors.append(LineError(line_no, line[:300], "duplicate of an earlier line"))
            continue
        seen.add(key)
        result.shifts.append(parsed)

    return result


def html_to_text(html: str) -> str:
    """Crude HTML -> text fallback for emails with no text/plain part."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">")
    )
    return "\n".join(part.strip(" |") for part in text.splitlines())
