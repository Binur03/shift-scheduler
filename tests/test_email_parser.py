"""Regex vendor-email parser: accepted formats and malformed-line handling."""
from __future__ import annotations

from datetime import date, time

import pytest

from utils.email_parser import html_to_text, parse_date, parse_shift_email, parse_time

TODAY = date(2026, 9, 14)


def one(line: str):
    return parse_shift_email(line, today=TODAY)


class TestTimes:
    @pytest.mark.parametrize("raw,expected", [
        ("4pm", time(16)), ("4 PM", time(16)), ("4:30 pm", time(16, 30)), ("4 p.m.", time(16)),
        ("12am", time(0)), ("12pm", time(12)), ("12:15 AM", time(0, 15)),
        ("16:00", time(16)), ("00:00", time(0)), ("7:05", time(7, 5)),
    ])
    def test_valid(self, raw, expected):
        assert parse_time(raw) == expected

    @pytest.mark.parametrize("raw", ["4", "16", "13pm", "0am", "25:00", "4:60 PM", "noon", ""])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            parse_time(raw)


class TestDates:
    @pytest.mark.parametrize("raw,expected", [
        ("09/20/2026", date(2026, 9, 20)), ("9/20/26", date(2026, 9, 20)), ("2026-09-20", date(2026, 9, 20)),
    ])
    def test_valid(self, raw, expected):
        assert parse_date(raw) == expected

    @pytest.mark.parametrize("raw", ["02/30/2026", "13/01/2026", "2026-00-10"])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            parse_date(raw)


class TestLineFormats:
    @pytest.mark.parametrize("line", [
        "09/20/2026 | 4:00 PM - 11:00 PM | Concessions Stand 12 | 6",
        "09/20/2026|4pm-11pm|Concessions Stand 12|6",
        "2026-09-20 | 16:00 - 23:00 | Concessions Stand 12 | Qty: 6",
        "09/20/2026 | 4:00 PM to 11:00 PM | Concessions Stand 12 | x6",
        "09/20/2026 | 4:00 PM – 11:00 PM | Concessions Stand 12 | 6 staff",
        "Date: 09/20/2026  Time: 4pm to 11pm  Position: Concessions Stand 12  Qty: 6",
        "date: 9/20/26, time: 16:00-23:00, role: Concessions Stand 12, headcount: 6",
    ])
    def test_accepted(self, line):
        result = one(line)
        assert result.errors == []
        (shift,) = result.shifts
        assert (shift.work_date, shift.start, shift.end, shift.position, shift.headcount) == (
            date(2026, 9, 20), time(16), time(23), "Concessions Stand 12", 6,
        )

    def test_overnight_shift(self):
        (shift,) = one("09/20/2026 | 10:00 PM - 6:00 AM | Cleanup Crew | 12").shifts
        assert shift.start == time(22) and shift.end == time(6)

    def test_position_whitespace_normalized(self):
        (shift,) = one("09/20/2026 | 4pm - 11pm |   Suite    Level   Bar  | 2").shifts
        assert shift.position == "Suite Level Bar"


class TestMalformedLines:
    @pytest.mark.parametrize("line,error_fragment", [
        ("09/20/2026 | 4 - 11 | Stand 12 | 6", "ambiguous"),
        ("09/20/2026 | 4:00 PM - 4:00 PM | Stand 12 | 6", "same"),
        ("02/30/2026 | 4pm - 11pm | Stand 12 | 6", "not a real calendar date"),
        ("09/20/2020 | 4pm - 11pm | Stand 12 | 6", "past"),
        ("09/20/2028 | 4pm - 11pm | Stand 12 | 6", "more than a year"),
        ("09/20/2026 | 4pm - 11pm | Stand 12 | 0", "headcount"),
        ("09/20/2026 | 4pm - 11pm | Stand 12 | 999", "headcount"),
        ("09/20/2026 | 13pm - 11pm | Stand 12 | 6", "12-hour"),
        ("09/20/2026 4pm-11pm Stand 12 six people", "couldn't read"),
        ("Sept 20: 4pm - 11pm, Stand 12, need 6 — 09/20/2026", "couldn't read"),
    ])
    def test_rejected_with_reason(self, line, error_fragment):
        result = one(line)
        assert result.shifts == []
        (error,) = result.errors
        assert error_fragment in error.error
        assert error.line_no == 1 and error.text

    def test_one_bad_line_does_not_sink_the_email(self):
        body = "\n".join([
            "09/20/2026 | 4pm - 11pm | Stand 12 | 6",
            "09/21/2026 | 4 - 11 | Stand 12 | 6",
            "09/22/2026 | 4pm - 11pm | Stand 12 | 6",
        ])
        result = parse_shift_email(body, today=TODAY)
        assert [s.line_no for s in result.shifts] == [1, 3]
        assert [e.line_no for e in result.errors] == [2]

    def test_duplicate_line_reported(self):
        line = "09/20/2026 | 4pm - 11pm | Stand 12 | 6"
        result = parse_shift_email(f"{line}\n{line}", today=TODAY)
        assert len(result.shifts) == 1
        assert "duplicate" in result.errors[0].error

    @pytest.mark.parametrize("noise", [
        "Hi team,", "Thanks! — Levy Ops", "Sent: 09/12/2026", "Call 303-555-0100 with questions",
        "", "   ", "Please confirm by 9/18.", "Doors open at 5pm.",
    ])
    def test_non_shift_lines_ignored_silently(self, noise):
        result = parse_shift_email(noise, today=TODAY)
        assert result.shifts == [] and result.errors == []

    def test_quoted_reply_lines_ignored(self):
        result = parse_shift_email("> 09/20/2026 | 4pm - 11pm | Stand 12 | 6", today=TODAY)
        assert result.shifts == [] and result.errors == []

    def test_empty_and_none_body(self):
        assert parse_shift_email("", today=TODAY).shifts == []
        assert parse_shift_email(None, today=TODAY).shifts == []  # type: ignore[arg-type]

    def test_pathological_input_is_fast(self):
        """Guard against catastrophic regex backtracking on hostile input."""
        import time as _time

        hostile = "09/20/2026 | " + "4" * 5000 + " - " + "|" * 5000
        started = _time.perf_counter()
        parse_shift_email(hostile * 20, today=TODAY)
        assert _time.perf_counter() - started < 2.0


def test_html_table_email_parses():
    html = """
    <p>Hello,</p>
    <table>
      <tr><th>Date</th><th>Time</th><th>Position</th><th>Qty</th></tr>
      <tr><td>09/20/2026</td><td>4:00 PM - 11:00 PM</td><td>Concessions Stand 12</td><td>6</td></tr>
    </table>
    <script>alert('x')</script>
    """
    result = parse_shift_email(html_to_text(html), today=TODAY)
    assert [s.position for s in result.shifts] == ["Concessions Stand 12"]
