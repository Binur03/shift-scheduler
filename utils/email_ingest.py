"""Persist and parse an inbound vendor email into DRAFT shifts.

Order of operations (each step committed so a failure later never loses
earlier work):

1. Dedupe on Message-ID. A redelivered email returns the stored record.
2. Store the raw email (committed) — recoverable even if parsing crashes.
3. Sender authentication: DKIM must pass for the vendor's domain and the
   From address must be on that domain; otherwise mark REJECTED, no shifts.
4. Parse lines, map positions to Jobs, create DRAFT shifts, record errors.

Input is the SendGrid Inbound Parse form payload (``from``, ``subject``,
``text``, ``html``, ``headers``, ``dkim``).
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import date

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    InboundEmail,
    Job,
    ParseStatus,
    Shift,
    ShiftSource,
    ShiftStatus,
    Vendor,
    utcnow_naive,
)
from utils.email_parser import html_to_text, parse_shift_email

logger = logging.getLogger(__name__)

_MESSAGE_ID_RE = re.compile(r"^message-id:\s*(<[^>\r\n]+>)", re.IGNORECASE | re.MULTILINE)
_DKIM_PASS_RE = re.compile(r"@([A-Za-z0-9.-]+)\s*:\s*pass", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"[\w.+'-]+@([A-Za-z0-9.-]+)")


def extract_message_id(fields: Mapping[str, str]) -> str:
    """Message-ID header, or a stable content hash when a sender omits it."""
    match = _MESSAGE_ID_RE.search(fields.get("headers", "") or "")
    if match:
        return match.group(1)[:255]
    digest = hashlib.sha256(
        "\x1f".join(
            fields.get(k, "") or "" for k in ("from", "subject", "text", "html")
        ).encode("utf-8", "replace")
    ).hexdigest()
    return f"<sha256-{digest}@no-message-id>"


def _domain_matches(domain: str, allowed: str) -> bool:
    domain, allowed = domain.lower().rstrip("."), allowed.lower().rstrip(".")
    return domain == allowed or domain.endswith("." + allowed)


def sender_authenticated(vendor: Vendor, fields: Mapping[str, str]) -> tuple[bool, str]:
    """DKIM pass for the vendor domain AND a From address on that domain."""
    passed = _DKIM_PASS_RE.findall(fields.get("dkim", "") or "")
    if not any(_domain_matches(d, vendor.allowed_sender_domain) for d in passed):
        return False, f"DKIM did not pass for {vendor.allowed_sender_domain}"
    from_match = _ADDRESS_RE.search(fields.get("from", "") or "")
    if not from_match or not _domain_matches(from_match.group(1), vendor.allowed_sender_domain):
        return False, f"From address is not on {vendor.allowed_sender_domain}"
    return True, ""


def create_drafts(
    record: InboundEmail,
    body: str,
    *,
    today: date,
    reference_date: date,
    default_job: Job | None,
) -> None:
    """Parse ``body`` into DRAFT shifts attached to ``record``; sets status/errors and commits.

    Job for each shift:
      * pipe/labeled lines name the job ("position") -> matched to a Job title;
      * shorthand lines ("9/5 20 @ 3pm parking") -> ``default_job``, with the
        optional area word stored on the shift, never invented when absent.
    """
    result = parse_shift_email(body, today=today, reference_date=reference_date)
    errors = [e.as_dict() for e in result.errors]
    jobs_by_title = {j.title.strip().lower(): j for j in Job.query.all()}

    created = 0
    for line in result.shifts:
        if line.position is not None:
            job = jobs_by_title.get(line.position.lower())
            missing = f"no job named '{line.position}' — add it on the Jobs page"
        else:
            job = default_job
            missing = "no job selected for this schedule — pick one and paste it again"
        if job is None:
            errors.append({"line": line.line_no, "text": line.text, "error": missing})
            continue

        duplicate = Shift.query.filter_by(
            job_id=job.id, date=line.work_date, start_time=line.start,
            end_time=line.end, area=line.area,
        ).first()
        if duplicate is not None:
            errors.append({
                "line": line.line_no, "text": line.text,
                "error": f"already scheduled (shift #{duplicate.id})",
            })
            continue
        db.session.add(
            Shift(
                job_id=job.id,
                date=line.work_date,
                start_time=line.start,
                end_time=line.end,     # None when the schedule gave no end
                area=line.area,        # None when the schedule gave no area
                required_headcount=line.headcount,
                status=ShiftStatus.DRAFT,
                source=ShiftSource.EMAIL,
                vendor_id=record.vendor_id,
                inbound_email_id=record.id,
            )
        )
        created += 1

    record.shifts_created = created
    record.errors = sorted(errors, key=lambda e: e["line"])
    if created and not errors:
        record.parse_status = ParseStatus.PARSED
    elif created:
        record.parse_status = ParseStatus.PARTIAL
    else:
        record.parse_status = ParseStatus.FAILED
    db.session.commit()


def ingest_pasted_schedule(
    text: str,
    *,
    job: Job,
    sent_on: date | None = None,
    subject: str = "",
    today: date | None = None,
) -> InboundEmail:
    """An admin pasted a vendor's schedule. The admin session is the
    authentication, so there is no DKIM step; each paste is its own record
    (repeat pastes can't double-book: duplicate shifts are skipped)."""
    today = today or utcnow_naive().date()
    record = InboundEmail(
        vendor_id=None,
        source="paste",
        message_id=f"<paste-{uuid.uuid4().hex}@shift-scheduler>",
        from_address="",
        subject=(subject or f"Pasted schedule for {job.title}")[:998],
        raw_body=text,
        dkim_result="",
        received_at_utc=utcnow_naive(),
        parse_status=ParseStatus.FAILED,
        errors=[],
    )
    db.session.add(record)
    db.session.commit()  # keep the raw text even if parsing fails
    create_drafts(record, text, today=today, reference_date=sent_on or today, default_job=job)
    logger.info(
        "Pasted schedule %s for job %s: %s, %d draft shift(s), %d error(s)",
        record.id, job.id, record.parse_status, record.shifts_created, len(record.errors),
    )
    return record


def ingest_vendor_email(
    vendor: Vendor, fields: Mapping[str, str], *, today: date | None = None
) -> tuple[InboundEmail, bool]:
    """Store + parse one email. Returns (record, created_now)."""
    message_id = extract_message_id(fields)
    existing = InboundEmail.query.filter_by(message_id=message_id).first()
    if existing is not None:
        return existing, False

    body = fields.get("text") or html_to_text(fields.get("html", "") or "")
    email = InboundEmail(
        vendor_id=vendor.id,
        message_id=message_id,
        from_address=(fields.get("from", "") or "")[:320],
        subject=(fields.get("subject", "") or "")[:998],
        raw_body=body,
        dkim_result=(fields.get("dkim", "") or "")[:512],
        received_at_utc=utcnow_naive(),
        parse_status=ParseStatus.FAILED,  # until parsing proves otherwise
        errors=[],
    )
    db.session.add(email)
    try:
        db.session.commit()  # (2) raw email is durable before parsing
    except IntegrityError:
        # A concurrent delivery of the same message won the insert.
        db.session.rollback()
        return InboundEmail.query.filter_by(message_id=message_id).one(), False

    ok, reason = sender_authenticated(vendor, fields)
    if not ok:
        email.parse_status = ParseStatus.REJECTED
        email.errors = [{"line": 0, "text": "", "error": reason}]
        db.session.commit()
        logger.warning("Rejected inbound email %s for vendor %s: %s", email.id, vendor.id, reason)
        return email, True

    today = today or utcnow_naive().date()
    create_drafts(email, body, today=today, reference_date=today, default_job=None)
    logger.info(
        "Inbound email %s (vendor %s): %s, %d draft shift(s), %d error(s)",
        email.id, vendor.id, email.parse_status, email.shifts_created, len(email.errors),
    )
    return email, True
