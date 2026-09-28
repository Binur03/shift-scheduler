"""Outbound SMS must stay inside the GSM-7 alphabet.

A single character outside it forces the whole message to UCS-2, cutting a
segment from 153 characters to 67. That roughly triples the per-message cost
and eats the Sole Proprietor brand's 3,000/day (1,000/day T-Mobile) cap.

These tests exist because it happened: an accented "Area" and a typographic
en-dash in the shift time range were silently costing 2.5x on every message.
"""
from __future__ import annotations

from datetime import date, time

import pytest

from utils.gsm7 import (
    is_gsm7,
    non_gsm7_characters,
    segment_count,
    to_gsm7,
)
from utils.sms import ShiftBroadcastDetails, WhatsAppService

TOKEN = "x" * 43
LANGUAGES = ("en", "es")


def _details(area="Parking", end=None):
    return ShiftBroadcastDetails(
        title="Ball Arena - Post-Event Cleaning",
        location_address="1000 Chopper Cir, Denver, CO 80204",
        work_date=date(2026, 9, 18),
        start_time=time(15, 0),
        end_time=end,
        estimated_hours=None,
        area=area,
    )


def _reminder_body(service, lang, end_time=time(23, 0)):
    captured = {}
    service._send = lambda to, **kw: captured.update(kw) or True
    service.send_shift_reminder(
        "+17205550101",
        title="Ball Arena - Post-Event Cleaning",
        location_address="1000 Chopper Cir, Denver, CO 80204",
        work_date=date(2026, 9, 18),
        start_time=time(15, 0),
        end_time=end_time,
        punch_url=f"https://example.test/punch/{TOKEN}",
        area="Parking",
        lang=lang,
    )
    return captured["body"]


class TestGsm7Helpers:
    def test_spanish_letters_inside_gsm7_are_kept(self):
        """GSM-7 really does contain these, so don't "fix" them."""
        assert is_gsm7("nino ñ ü ¿Listo? ¡Si! é à")

    @pytest.mark.parametrize("char", ["á", "í", "ó", "ú", "😀"])
    def test_characters_outside_gsm7_are_detected(self, char):
        assert not is_gsm7(f"turno {char}")
        assert char in non_gsm7_characters(f"turno {char}")

    @pytest.mark.parametrize(
        "raw,expected",
        [("3:00 PM – 11:00 PM", "3:00 PM - 11:00 PM"),
         ("hoy — manana", "hoy - manana"),
         ("dijo “si”", 'dijo "si"'),
         ("espere…", "espere..."),
         ("a b", "a b")],
    )
    def test_typographic_punctuation_is_normalised(self, raw, expected):
        assert to_gsm7(raw) == expected

    def test_accents_are_never_stripped(self):
        """Mangling "años" into "anos" would be worse than the extra segment."""
        assert to_gsm7("María cumplió años") == "María cumplió años"

    def test_segment_boundaries(self):
        assert segment_count("a" * 160) == 1
        assert segment_count("a" * 161) == 2
        assert segment_count("á") == 1            # UCS-2, single
        assert segment_count("á" + "a" * 70) == 2  # UCS-2, over 70 units

    def test_empty_text_is_safe(self):
        assert to_gsm7("") == ""


class TestOutboundBodiesAreGsm7:
    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_invitation(self, app, lang):
        body = to_gsm7(WhatsAppService()._format_body(_details(), TOKEN, lang))
        assert is_gsm7(body), f"forced to UCS-2 by {non_gsm7_characters(body)}"
        assert segment_count(body) <= 2

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_reminder(self, app, lang):
        body = to_gsm7(_reminder_body(WhatsAppService(), lang))
        assert is_gsm7(body), f"forced to UCS-2 by {non_gsm7_characters(body)}"
        assert segment_count(body) <= 3

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_invitation_without_an_area(self, app, lang):
        body = to_gsm7(WhatsAppService()._format_body(_details(area=None), TOKEN, lang))
        assert is_gsm7(body)

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_open_ended_shift_reminder(self, app, lang):
        """No end time means no dash at all — still must be clean."""
        body = to_gsm7(_reminder_body(WhatsAppService(), lang, end_time=None))
        assert is_gsm7(body)

    def test_spanish_costs_no_more_than_english(self, app):
        """The whole point: Spanish workers shouldn't cost 2.5x to message."""
        service = WhatsAppService()
        english = to_gsm7(service._format_body(_details(), TOKEN, "en"))
        spanish = to_gsm7(service._format_body(_details(), TOKEN, "es"))
        assert segment_count(spanish) <= segment_count(english)


class TestSanitiserIsWiredIn:
    def test_sms_channel_sanitises_the_body(self, app, monkeypatch):
        """An en-dash reaching the wire would triple the segment count."""
        service = WhatsAppService(channel="sms")
        sent = {}

        class FakeMessages:
            def create(self, **kwargs):
                sent.update(kwargs)
                return type("M", (), {"sid": "SM123"})()

        monkeypatch.setattr(
            service, "_client", type("C", (), {"messages": FakeMessages()})()
        )
        monkeypatch.setattr(service, "sms_number", "+17205550100")

        service._send("+17205550101", body="3:00 PM – 11:00 PM")
        assert sent["body"] == "3:00 PM - 11:00 PM"
        assert is_gsm7(sent["body"])

    def test_whatsapp_channel_is_left_alone(self, app, monkeypatch):
        """WhatsApp is UTF-8; sanitising there would degrade text for nothing."""
        service = WhatsAppService(channel="whatsapp")
        sent = {}

        class FakeMessages:
            def create(self, **kwargs):
                sent.update(kwargs)
                return type("M", (), {"sid": "SM123"})()

        monkeypatch.setattr(
            service, "_client", type("C", (), {"messages": FakeMessages()})()
        )
        monkeypatch.setattr(service, "whatsapp_number", "+17205550100")

        service._send("+17205550101", body="3:00 PM – 11:00 PM")
        assert sent["body"] == "3:00 PM – 11:00 PM"


class TestCatalogSmsStrings:
    """SMS-facing Spanish copy is authored accent-free on purpose."""

    SMS_KEYS = [
        "New shift available: {title}",
        "Area: {area}",
        "Location: {address}",
        "Date: {date}",
        "Time: {time}",
        "Tap to accept (first come, first served):",
        "Reminder: you're confirmed for {title}",
        "Check in when you arrive: {url}",
        "If you can no longer make it, contact your coordinator ASAP.",
        "Your PIN is the last 4 digits of your phone number.",
        "Accept (first come, first served):",
        "Reply STOP to opt out.",
        "{count} new shifts available at {job}.",
        "Tap to pick the days you can work:",
    ]

    def test_spanish_sms_strings_are_gsm7(self):
        from utils.i18n import CATALOG

        offenders = {
            key: "".join(sorted(non_gsm7_characters(CATALOG["es"][key])))
            for key in self.SMS_KEYS
            if not is_gsm7(CATALOG["es"][key])
        }
        assert not offenders, (
            "these Spanish SMS strings force UCS-2 and triple the segment cost: "
            f"{offenders}"
        )

    def test_web_spanish_may_keep_its_accents(self):
        """Only SMS copy is constrained; pages should be spelled properly."""
        from utils.i18n import CATALOG

        assert not is_gsm7(CATALOG["es"]["You're confirmed!"])
