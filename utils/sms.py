"""Production WhatsApp/SMS integration via Twilio.

Wraps ``twilio.rest.Client`` in a small service object so the rest of the
application depends on an interface rather than the SDK directly. The broadcast
loop is deliberately fault-tolerant: a failure delivering to one worker is
logged and skipped so the remaining workers still receive their invitations.

Channel selection
-----------------
``MESSAGING_CHANNEL`` picks the transport:

    whatsapp  (default) — richer UX, but business-initiated messages require
              a Meta-verified sender and approved templates (see below).
    sms       — plain text messages from ``TWILIO_SMS_NUMBER``. No message
              templates or Meta approval needed, so this is the fastest way
              to production (US senders still need toll-free verification or
              A2P 10DLC registration in the Twilio console).

Message templates (WhatsApp production only)
--------------------------------------------
WhatsApp only allows *business-initiated* messages that use a Meta-approved
template. Free-form ``body`` text works only in the Twilio sandbox or inside
a 24-hour customer-service window after a worker messages you. For production,
create three Content Templates in the Twilio console (see TWILIO_SETUP.md for
the exact template text to submit) and set their SIDs:

    TWILIO_CONTENT_SID_INVITE     shift invitation (with accept-link button)
    TWILIO_CONTENT_SID_REMINDER   day-before worker reminder
    TWILIO_CONTENT_SID_ALERT      admin understaffing alert

When a template SID is set, messages of that type are sent via
``content_sid`` + ``content_variables``. When it is blank the service falls
back to a free-form body (fine for the sandbox and local development).
SMS always sends the free-form body — templates don't apply to SMS.

Credentials are read from the environment:
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_NUMBER   (E.164, e.g. +14155238886; whatsapp channel)
    TWILIO_SMS_NUMBER        (E.164; sms channel)

The public acceptance domain is read from PUBLIC_BASE_URL (e.g.
https://shifts.example.com); links are rendered as
``https://<domain>/accept/<token>``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, time
from typing import TYPE_CHECKING, Iterable

from models import format_shift_time
from utils.gsm7 import to_gsm7

if TYPE_CHECKING:  # avoid import cycle / hard SDK dependency at type-check time
    from models import ShiftAssignment

logger = logging.getLogger(__name__)

# Meta rejects template variables containing newlines, tabs, or 4+ consecutive
# spaces — collapse any such whitespace before sending.
_TEMPLATE_VAR_WS = re.compile(r"[\r\n\t]+|\s{4,}")


def _template_var(value: object) -> str:
    """Render a value as a Meta-safe template variable (single-line string)."""
    return _TEMPLATE_VAR_WS.sub(" ", str(value)).strip()


@dataclass(frozen=True)
class ShiftBroadcastDetails:
    """Immutable shift facts needed to render a broadcast message body."""

    title: str
    location_address: str
    work_date: date
    start_time: time
    end_time: time | None          # None = no scheduled end ("until the job is done")
    estimated_hours: float | None
    area: str | None = None        # e.g. "Parking"; shown only when given

    @property
    def date_text(self) -> str:
        return f"{self.work_date:%a %d %b %Y}"

    @property
    def time_text(self) -> str:
        return format_shift_time(self.start_time, self.end_time)


@dataclass(frozen=True)
class BroadcastResult:
    """Summary of a broadcast run."""

    sent: int
    failed: int
    total: int
    # Workers whose message did not go out, so the console can name them.
    # A count alone leaves a manager with nobody to call.
    failed_names: tuple[str, ...] = ()


class WhatsAppService:
    """Send shift broadcasts to workers over WhatsApp using Twilio."""

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        whatsapp_number: str | None = None,
        base_url: str | None = None,
        channel: str | None = None,
        sms_number: str | None = None,
        messaging_service_sid: str | None = None,
    ) -> None:
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID")
        self.auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
        self.whatsapp_number = whatsapp_number or os.environ.get(
            "TWILIO_WHATSAPP_NUMBER"
        )
        self.channel = (
            channel or os.environ.get("MESSAGING_CHANNEL", "whatsapp")
        ).strip().lower()
        self.sms_number = sms_number or os.environ.get("TWILIO_SMS_NUMBER")
        # When set, SMS goes out through the Messaging Service that the A2P
        # 10DLC campaign is registered against, rather than a bare number.
        self.messaging_service_sid = messaging_service_sid or os.environ.get(
            "TWILIO_MESSAGING_SERVICE_SID"
        )
        self.base_url = (
            base_url or os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080")
        ).rstrip("/")
        self.invite_content_sid = os.environ.get("TWILIO_CONTENT_SID_INVITE")
        self.reminder_content_sid = os.environ.get("TWILIO_CONTENT_SID_REMINDER")
        self.alert_content_sid = os.environ.get("TWILIO_CONTENT_SID_ALERT")
        self._client = self._build_client()
        if (
            self._client is not None
            and self.channel == "whatsapp"
            and not self.invite_content_sid
        ):
            logger.warning(
                "TWILIO_CONTENT_SID_INVITE is not set; falling back to free-form "
                "WhatsApp bodies. This only works in the sandbox or inside a "
                "24h reply window — set the approved template SIDs for "
                "production, or use MESSAGING_CHANNEL=sms."
            )

    # ------------------------------------------------------------------ #
    # Client construction
    # ------------------------------------------------------------------ #
    def _build_client(self):
        """Construct a Twilio client, or return ``None`` if unconfigured.

        Returning ``None`` keeps the app importable and runnable in
        environments without credentials (local dev, CI); ``send_shift_broadcast``
        degrades to logging instead of raising at import time.
        """
        if self.channel == "sms":
            # Either is a usable sender; the service takes precedence below.
            sender = self.messaging_service_sid or self.sms_number
        else:
            sender = self.whatsapp_number
        if not (self.account_sid and self.auth_token and sender):
            logger.warning(
                "Messaging service is not fully configured for channel=%s "
                "(missing SID/token/sender number); messages will be logged, "
                "not sent.",
                self.channel,
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
    # Low-level send
    # ------------------------------------------------------------------ #
    def _send(
        self,
        to: str,
        *,
        body: str,
        content_sid: str | None = None,
        content_variables: dict[str, str] | None = None,
        context: str = "message",
    ) -> bool:
        """Send one WhatsApp message; returns True on success, never raises.

        If ``content_sid`` is provided the message is sent as an approved
        Content Template with ``content_variables``; otherwise ``body`` is
        sent free-form (sandbox / 24h-window only).
        """
        if not to:
            logger.error("%s not sent: no destination number.", context)
            return False
        if self._client is None:
            logger.info(
                "[%s STUB] %s to=%s body=%s", self.channel.upper(), context, to, body
            )
            return False
        try:
            if self.channel == "sms":
                # SMS has no template mechanism — always the plain body.
                # Typographic punctuation would force the whole message to
                # UCS-2 and roughly triple its segment count, so normalise it
                # here, where every SMS body passes through. WhatsApp is UTF-8
                # and needs no such treatment.
                kwargs: dict[str, str] = {"to": to, "body": to_gsm7(body)}
                if self.messaging_service_sid:
                    kwargs["messaging_service_sid"] = self.messaging_service_sid
                else:
                    kwargs["from_"] = self.sms_number
            else:
                kwargs = {
                    "to": f"whatsapp:{to}",
                    "from_": f"whatsapp:{self.whatsapp_number}",
                }
                if content_sid:
                    kwargs["content_sid"] = content_sid
                    kwargs["content_variables"] = json.dumps(content_variables or {})
                else:
                    kwargs["body"] = body
            message = self._client.messages.create(**kwargs)
            logger.info(
                "Sent %s %s sid=%s to=%s template=%s",
                self.channel,
                context,
                message.sid,
                to,
                kwargs.get("content_sid", "none (free-form)"),
            )
            return True
        except Exception:  # noqa: BLE001 - callers rely on never raising
            logger.exception(
                "Failed to send %s %s to %s.", self.channel, context, to
            )
            return False

    # ------------------------------------------------------------------ #
    # Shift invitations (broadcast)
    # ------------------------------------------------------------------ #
    def _brand_prefix(self) -> str:
        """"Rohan Binu: " so a worker knows instantly who is texting them.

        Carriers filter unbranded traffic, and The Campaign Registry expects
        the sender to be identifiable in the message itself. Empty when no
        business name is configured, so nothing reads "None: ".
        """
        name = os.environ.get("BUSINESS_NAME", "").strip()
        return f"{name}: " if name else ""

    def _stop_suffix(self, lang: str = "en") -> str:
        """Opt-out line. Required on SMS; WhatsApp has its own block controls."""
        from utils.i18n import translate

        if self.channel != "sms":
            return ""
        return "\n" + translate("Reply STOP to opt out.", lang)

    def _accept_url(self, token: str) -> str:
        return f"{self.base_url}/accept/{token}"

    def _format_body(
        self, details: ShiftBroadcastDetails, token: str, lang: str = "en"
    ) -> str:
        """Render the free-form fallback body for one invitation, in ``lang``."""
        from utils.i18n import format_date_short, translate

        hours = (
            " " + translate("(~{hours} hrs)", lang, hours=f"{details.estimated_hours:g}")
            if details.estimated_hours
            else ""
        )
        date_text = format_date_short(details.work_date, lang=lang, year=True)
        return (
            self._brand_prefix()
            + translate("New shift available: {title}", lang, title=details.title) + "\n"
            + (translate("Area: {area}", lang, area=details.area) + "\n" if details.area else "")
            + translate("Location: {address}", lang, address=details.location_address) + "\n"
            + translate("Date: {date}", lang, date=date_text) + "\n"
            + translate("Time: {time}", lang, time=details.time_text) + hours + "\n\n"
            + translate("Accept (first come, first served):", lang) + "\n"
            + self._accept_url(token)
            + self._stop_suffix(lang)
        )

    def _invite_variables(
        self, details: ShiftBroadcastDetails, token: str
    ) -> dict[str, str]:
        """Variables for the invite template.

        {{1}} job title, {{2}} location, {{3}} date, {{4}} time range,
        {{5}} estimated hours, {{6}} acceptance token (used as the dynamic
        suffix of the template's URL button: <base>/accept/{{6}}).
        """
        return {
            "1": _template_var(details.title),
            "2": _template_var(details.location_address),
            "3": details.date_text,
            "4": details.time_text,
            # Template variables can't be empty; open-ended shifts say so.
            "5": f"{details.estimated_hours:g}" if details.estimated_hours else "until done",
            "6": token,
        }

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
        failed_names: list[str] = []

        for assignment in assignments:
            total += 1
            phone = getattr(assignment.employee, "phone_number", None)
            worker_name = getattr(assignment.employee, "full_name", None) or "?"
            if not phone:
                failed += 1
                failed_names.append(worker_name)
                logger.error(
                    "Skipping assignment id=%s: employee has no phone number.",
                    getattr(assignment, "id", "?"),
                )
                continue

            worker_lang = getattr(assignment.employee, "language", None) or "en"
            ok = self._send(
                phone,
                body=self._format_body(job_details, assignment.token, worker_lang),
                content_sid=self.invite_content_sid,
                content_variables=self._invite_variables(
                    job_details, assignment.token
                ),
                context=f"invitation (assignment={getattr(assignment, 'id', '?')})",
            )
            if ok:
                sent += 1
            else:
                failed += 1
                failed_names.append(worker_name)

        logger.info(
            "Broadcast complete: sent=%d failed=%d total=%d", sent, failed, total
        )
        return BroadcastResult(
            sent=sent, failed=failed, total=total, failed_names=tuple(failed_names)
        )

    # ------------------------------------------------------------------ #
    # Day-before worker reminder
    # ------------------------------------------------------------------ #
    def send_shift_reminder(
        self,
        phone: str,
        *,
        title: str,
        location_address: str,
        work_date: date,
        start_time: time,
        end_time: time | None,
        punch_url: str | None = None,
        area: str | None = None,
        lang: str = "en",
    ) -> bool:
        """Remind one confirmed worker about their upcoming shift.

        Template variables: {{1}} job title, {{2}} location, {{3}} date,
        {{4}} time range. ``punch_url`` (the PIN check-in keypad link) is
        included in the SMS / free-form body; the approved WhatsApp template
        has no slot for it yet.
        """
        from utils.i18n import format_date_short, translate

        date_text = format_date_short(work_date, lang=lang)
        time_text = format_shift_time(start_time, end_time)
        body = (
            self._brand_prefix()
            + translate("Reminder: you're confirmed for {title}", lang, title=title) + "\n"
            + (translate("Area: {area}", lang, area=area) + "\n" if area else "")
            + translate("Location: {address}", lang, address=location_address) + "\n"
            + f"{date_text} {time_text}" + "\n\n"
            + (
                translate("Check in when you arrive: {url}", lang, url=punch_url) + "\n\n"
                if punch_url else ""
            )
            + translate(
                "If you can no longer make it, contact your coordinator ASAP.", lang
            )
            + self._stop_suffix(lang)
        )
        return self._send(
            phone,
            body=body,
            content_sid=self.reminder_content_sid,
            content_variables={
                "1": _template_var(title),
                "2": _template_var(location_address),
                "3": date_text,
                "4": time_text,
            },
            context="reminder",
        )

    # ------------------------------------------------------------------ #
    # Admin alerts
    # ------------------------------------------------------------------ #
    def send_staffing_alert(
        self,
        *,
        title: str,
        location_address: str,
        work_date: date,
        start_time: time,
        end_time: time | None,
        accepted: int,
        required: int,
        admin_number: str | None = None,
    ) -> bool:
        """Alert the admin that one shift is understaffed.

        Template variables: {{1}} job title, {{2}} location, {{3}} date,
        {{4}} time range, {{5}} confirmed count, {{6}} required count.
        """
        admin_number = admin_number or os.environ.get("ADMIN_WHATSAPP_NUMBER")
        if not admin_number:
            logger.error("ADMIN_WHATSAPP_NUMBER not set; cannot send admin alert.")
            return False
        date_text = f"{work_date:%a %d %b}"
        time_text = format_shift_time(start_time, end_time)
        body = (
            f"⚠️ Staffing alert: {title} @ {location_address}\n"
            f"{date_text} {time_text} — {accepted}/{required} confirmed "
            f"(need {required - accepted} more).\n\n"
            "Open the dashboard to adjust the schedule or re-dispatch."
        )
        return self._send(
            admin_number,
            body=body,
            content_sid=self.alert_content_sid,
            content_variables={
                "1": _template_var(title),
                "2": _template_var(location_address),
                "3": date_text,
                "4": time_text,
                "5": str(accepted),
                "6": str(required),
            },
            context="staffing alert",
        )

    def send_admin_alert(self, body: str, admin_number: str | None = None) -> bool:
        """Send a free-form WhatsApp alert to the admin (ADMIN_WHATSAPP_NUMBER).

        Free-form messages only deliver inside a 24h reply window in
        production — prefer ``send_staffing_alert`` (templated) for the
        scheduled understaffing sweep.
        """
        admin_number = admin_number or os.environ.get("ADMIN_WHATSAPP_NUMBER")
        if not admin_number:
            logger.error("ADMIN_WHATSAPP_NUMBER not set; cannot send admin alert.")
            return False
        return self._send(admin_number, body=body, context="admin alert")

    def send_message(self, to: str, body: str) -> bool:
        """Send one free-form WhatsApp message; returns True on success."""
        return self._send(to, body=body)
