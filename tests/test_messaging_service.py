"""Sending through the A2P Messaging Service rather than a bare number.

The service SID below is a placeholder: the real one lives in the Cloud Run
environment, not in source.

10DLC binds the campaign to a Messaging Service. Sending with
``messaging_service_sid`` lets Twilio choose the sender and apply opt-out
state; sending with a bare ``from_`` bypasses that. The fallback to a plain
number is kept so local dev and the WhatsApp channel are unaffected.
"""
from __future__ import annotations

import pytest

from utils.sms import WhatsAppService


class _Recorder:
    """Stands in for twilio.rest.Client, capturing what would be sent."""

    def __init__(self):
        self.sent: dict = {}
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.sent = kwargs
                return type("M", (), {"sid": "SMtest"})()

        self.messages = Messages()


def _service(monkeypatch, **env) -> tuple[WhatsAppService, _Recorder]:
    for key in ("TWILIO_MESSAGING_SERVICE_SID", "TWILIO_SMS_NUMBER",
                "TWILIO_WHATSAPP_NUMBER", "MESSAGING_CHANNEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    svc = WhatsAppService()
    recorder = _Recorder()
    monkeypatch.setattr(svc, "_client", recorder)
    return svc, recorder


class TestSendingSender:
    def test_messaging_service_is_preferred(self, app, monkeypatch):
        svc, rec = _service(
            monkeypatch,
            MESSAGING_CHANNEL="sms",
            TWILIO_SMS_NUMBER="+17207296593",
            TWILIO_MESSAGING_SERVICE_SID="MG00000000000000000000000000000001",
        )
        svc._send("+17205550101", body="hello")

        assert rec.sent["messaging_service_sid"] == "MG00000000000000000000000000000001"
        assert "from_" not in rec.sent, "a bare from_ bypasses the campaign's service"
        assert rec.sent["to"] == "+17205550101"

    def test_falls_back_to_the_number(self, app, monkeypatch):
        """Local dev and any account without a service must still work."""
        svc, rec = _service(
            monkeypatch, MESSAGING_CHANNEL="sms", TWILIO_SMS_NUMBER="+17207296593"
        )
        svc._send("+17205550101", body="hello")

        assert rec.sent["from_"] == "+17207296593"
        assert "messaging_service_sid" not in rec.sent

    def test_whatsapp_ignores_the_messaging_service(self, app, monkeypatch):
        """The service is an SMS/10DLC concept; WhatsApp addresses its own sender."""
        svc, rec = _service(
            monkeypatch,
            MESSAGING_CHANNEL="whatsapp",
            TWILIO_WHATSAPP_NUMBER="+17207296593",
            TWILIO_MESSAGING_SERVICE_SID="MG00000000000000000000000000000001",
        )
        svc._send("+17205550101", body="hello")

        assert rec.sent["from_"] == "whatsapp:+17207296593"
        assert "messaging_service_sid" not in rec.sent

    def test_body_is_still_sanitised_through_the_service(self, app, monkeypatch):
        """The GSM-7 normalisation must not be lost on the new code path."""
        svc, rec = _service(
            monkeypatch,
            MESSAGING_CHANNEL="sms",
            TWILIO_MESSAGING_SERVICE_SID="MG00000000000000000000000000000001",
        )
        svc._send("+17205550101", body="3:00 PM – 11:00 PM")

        assert rec.sent["body"] == "3:00 PM - 11:00 PM"


class TestConfiguration:
    def test_a_service_alone_counts_as_configured(self, app, monkeypatch):
        """With 10DLC the service is the sender; no bare number is required."""
        for key in ("TWILIO_SMS_NUMBER", "TWILIO_WHATSAPP_NUMBER"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("MESSAGING_CHANNEL", "sms")
        monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", "MGtest")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")

        assert WhatsAppService().is_configured

    def test_neither_sender_means_unconfigured(self, app, monkeypatch):
        for key in ("TWILIO_SMS_NUMBER", "TWILIO_MESSAGING_SERVICE_SID",
                    "TWILIO_WHATSAPP_NUMBER"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("MESSAGING_CHANNEL", "sms")

        assert not WhatsAppService().is_configured

    def test_explicit_argument_beats_the_environment(self, app, monkeypatch):
        monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", "MGfromenv")
        assert WhatsAppService(messaging_service_sid="MGexplicit").messaging_service_sid == (
            "MGexplicit"
        )
