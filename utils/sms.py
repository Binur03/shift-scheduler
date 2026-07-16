"""Production WhatsApp integration via Twilio.

Wraps ``twilio.rest.Client`` in a small service object so the rest of the
application depends on an interface rather than the SDK directly. The broadcast
loop is deliberately fault-tolerant: a failure delivering to one worker is
logged and skipped so the remaining workers still receive their invitations.

Credentials are read from the environment:
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_NUMBER   (E.164, e.g. +14155238886)

The public acceptance domain is read from PUBLIC_BASE_URL (e.g.
https://shifts.example.com); links are rendered as
``https://<domain>/accept/<token>``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, time
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # avoid import cycle / hard SDK dependency at type-check time
    from models import ShiftAssignment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShiftBroadcastDetails:
    """Immutable shift facts needed to render a broadcast message body."""

    title: str
    location_address: str
    work_date: date
    start_time: time
    end_time: time
    estimated_hours: float


@dataclass(frozen=True)
class BroadcastResult:
    """Summary of a broadcast run."""

    sent: int
    failed: int
    total: int


class WhatsAppService:
    """Send shift broadcasts to workers over WhatsApp using Twilio."""

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        whatsapp_number: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID")
        self.auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
        self.whatsapp_number = whatsapp_number or os.environ.get(
            "TWILIO_WHATSAPP_NUMBER"
        )
        self.base_url = (
            base_url or os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080")
        ).rstrip("/")
        self._client = self._build_client()

    # ------------------------------------------------------------------ #
    # Client construction
    # ------------------------------------------------------------------ #
    def _build_client(self):
        """Construct a Twilio client, or return ``None`` if unconfigured.

        Returning ``None`` keeps the app importable and runnable in
        environments without credentials (local dev, CI); ``send_shift_broadcast``
        degrades to logging instead of raising at import time.
        """
        if not (self.account_sid and self.auth_token and self.whatsapp_number):
            logger.warning(
                "WhatsAppService is not fully configured "
                "(missing SID/token/number); messages will be logged, not sent."
            )
            return None
        try:
            from twilio.rest import Client
        except ImportError:
            logger.error("twilio package not installed; WhatsApp sending disabled.")
            return None
        return Client(self.account_sid, self.auth_token)

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------ #
    # Message formatting
    # ------------------------------------------------------------------ #
    def _accept_url(self, token: str) -> str:
        return f"{self.base_url}/accept/{token}"

    def _format_body(
        self, details: ShiftBroadcastDetails, token: str
    ) -> str:
        """Render a clear, plain-text WhatsApp body for one assignment."""
        return (
            f"New shift available: {details.title}\n"
            f"Location: {details.location_address}\n"
            f"Date: {details.work_date:%a %d %b %Y}\n"
            f"Time: {details.start_time:%H:%M}-{details.end_time:%H:%M} "
            f"(~{details.estimated_hours:g} hrs)\n\n"
            f"Tap to accept (first come, first served):\n"
            f"{self._accept_url(token)}"
        )

    # ------------------------------------------------------------------ #
    # Broadcast
    # ------------------------------------------------------------------ #
    def send_shift_broadcast(
        self,
        assignments: Iterable["ShiftAssignment"],
        job_details: ShiftBroadcastDetails,
    ) -> BroadcastResult:
        """Send the acceptance link to every assignment's worker.

        Iterates safely: an exception on a single send is caught, logged, and
        the loop continues so other workers still receive their message.

        Args:
            assignments: pending ``ShiftAssignment`` rows (each must expose
                ``token`` and ``employee.phone_number``).
            job_details: the shift facts used to render the message body.

        Returns:
            A ``BroadcastResult`` with sent / failed / total counts.
        """
        sent = 0
        failed = 0
        total = 0

        for assignment in assignments:
            total += 1
            phone = getattr(assignment.employee, "phone_number", None)
            if not phone:
                failed += 1
                logger.error(
                    "Skipping assignment id=%s: employee has no phone number.",
                    getattr(assignment, "id", "?"),
                )
                continue

            body = self._format_body(job_details, assignment.token)

            if self._client is None:
                # Unconfigured environment: log and count as failed-to-send.
                logger.info("[WHATSAPP STUB] to=%s body=%s", phone, body)
                failed += 1
                continue

            try:
                message = self._client.messages.create(
                    to=f"whatsapp:{phone}",
                    from_=f"whatsapp:{self.whatsapp_number}",
                    body=body,
                )
                sent += 1
                logger.info(
                    "Sent WhatsApp sid=%s to=%s assignment=%s",
                    message.sid,
                    phone,
                    getattr(assignment, "id", "?"),
                )
            except Exception:  # noqa: BLE001 - resilient broadcast, keep looping
                failed += 1
                logger.exception(
                    "Failed to send WhatsApp to %s for assignment id=%s; "
                    "continuing with remaining workers.",
                    phone,
                    getattr(assignment, "id", "?"),
                )

        logger.info(
            "Broadcast complete: sent=%d failed=%d total=%d", sent, failed, total
        )
        return BroadcastResult(sent=sent, failed=failed, total=total)

    # ------------------------------------------------------------------ #
    # Single messages
    # ------------------------------------------------------------------ #
    def send_message(self, to: str, body: str) -> bool:
        """Send one WhatsApp message; returns True on success, never raises."""
        if not to:
            logger.error("send_message called without a destination number.")
            return False
        if self._client is None:
            logger.info("[WHATSAPP STUB] to=%s body=%s", to, body)
            return False
        try:
            message = self._client.messages.create(
                to=f"whatsapp:{to}",
                from_=f"whatsapp:{self.whatsapp_number}",
                body=body,
            )
            logger.info("Sent WhatsApp sid=%s to=%s", message.sid, to)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send WhatsApp to %s.", to)
            return False

    # ------------------------------------------------------------------ #
    # Admin alerts
    # ------------------------------------------------------------------ #
    def send_admin_alert(self, body: str, admin_number: str | None = None) -> bool:
        """Send a WhatsApp alert to the admin (ADMIN_WHATSAPP_NUMBER).

        Returns True if the message was handed to Twilio successfully,
        False otherwise (unconfigured service, missing admin number, or a
        Twilio error — all logged, never raised).
        """
        admin_number = admin_number or os.environ.get("ADMIN_WHATSAPP_NUMBER")
        if not admin_number:
            logger.error("ADMIN_WHATSAPP_NUMBER not set; cannot send admin alert.")
            return False
        if self._client is None:
            logger.info("[WHATSAPP STUB] admin alert to=%s body=%s", admin_number, body)
            return False
        try:
            message = self._client.messages.create(
                to=f"whatsapp:{admin_number}",
                from_=f"whatsapp:{self.whatsapp_number}",
                body=body,
            )
            logger.info("Sent admin alert sid=%s to=%s", message.sid, admin_number)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send admin alert to %s.", admin_number)
            return False
