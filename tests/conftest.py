"""Shared pytest fixtures: an app bound to an in-memory SQLite database."""
from __future__ import annotations

import base64
import os
import sys
import uuid
from datetime import date, datetime, time, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")
os.environ.setdefault("CRON_SECRET", "test-cron-secret")
# Webhook credentials are *forced* (not setdefault) so a developer's real
# Twilio/SendGrid secrets in the shell can never leak into test signing.
TWILIO_TEST_TOKEN = "test-twilio-auth-token"
TWILIO_TEST_SID = "ACtest00000000000000000000000000"
os.environ["TWILIO_AUTH_TOKEN"] = TWILIO_TEST_TOKEN
os.environ["TWILIO_ACCOUNT_SID"] = TWILIO_TEST_SID
os.environ["INBOUND_EMAIL_USERNAME"] = "sendgrid"
os.environ["INBOUND_EMAIL_PASSWORD"] = "test-inbound-pass"
os.environ["PUNCH_EARLY_MINUTES"] = "60"
os.environ["PIN_PEPPER"] = "test-pin-pepper-not-for-production"

from twilio.request_validator import RequestValidator  # noqa: E402

from app import create_app  # noqa: E402
from config import TestingConfig  # noqa: E402
from extensions import db  # noqa: E402
from models import (  # noqa: E402
    AssignmentStatus,
    Employee,
    Job,
    Shift,
    ShiftAssignment,
    ShiftStatus,
    Vendor,
    utcnow_naive,
)
from utils.timeutil import utc_naive_to_local  # noqa: E402

VENUE_TZ = "America/Denver"
WORKER_PHONE = "+13035550199"
SMS_PATH = "/webhooks/twilio/sms"


# --------------------------------------------------------------------------- #
# Twilio request signing
# --------------------------------------------------------------------------- #
def sms_params(body: str, *, sid: str | None = None, from_: str = WORKER_PHONE,
               account_sid: str = TWILIO_TEST_SID) -> dict:
    """Form fields shaped like a real Twilio inbound-SMS webhook."""
    return {
        "MessageSid": sid or f"SM{uuid.uuid4().hex}",
        "AccountSid": account_sid,
        "From": from_,
        "To": "+18447135873",
        "Body": body,
        "NumMedia": "0",
    }


def twilio_signature(app, params: dict, path: str = SMS_PATH,
                     token: str = TWILIO_TEST_TOKEN) -> str:
    url = app.config["PUBLIC_BASE_URL"].rstrip("/") + path
    return RequestValidator(token).compute_signature(url, params)


@pytest.fixture()
def post_sms(app):
    """post_sms(body, sid=..., from_=..., signed=True, client=None) -> Response."""

    def _post(body: str, *, sid: str | None = None, from_: str = WORKER_PHONE,
              signed: bool = True, client=None, params: dict | None = None):
        fields = params or sms_params(body, sid=sid, from_=from_)
        headers = {"X-Twilio-Signature": twilio_signature(app, fields)} if signed else {}
        return (client or app.test_client()).post(SMS_PATH, data=fields, headers=headers)

    return _post


# --------------------------------------------------------------------------- #
# Punch fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def venue_job(app):
    job = Job(title="Concessions Stand 12", location_address="Ball Arena", timezone=VENUE_TZ)
    db.session.add(job)
    db.session.commit()
    return job


@pytest.fixture()
def worker(app):
    employee = Employee(first_name="Maya", last_name="Punch", phone_number=WORKER_PHONE)
    db.session.add(employee)
    db.session.commit()
    return employee


@pytest.fixture()
def make_assignment(app, venue_job, worker):
    """Create a shift + assignment starting ``start_in`` from now (venue-local).

    make_assignment(start_in=timedelta(minutes=-30), hours=4,
                    status=AssignmentStatus.accepted, shift_status=ShiftStatus.OPEN)
    """

    def _make(*, start_in=timedelta(minutes=-30), hours=4.0, employee=None,
              status=AssignmentStatus.accepted, shift_status=ShiftStatus.OPEN,
              job=None, now_utc: datetime | None = None):
        job = job or venue_job
        now_local = utc_naive_to_local(now_utc or utcnow_naive(), job.timezone)
        start_local = (now_local + start_in).replace(second=0, microsecond=0, tzinfo=None)
        end_local = start_local + timedelta(hours=hours)
        shift = Shift(
            job_id=job.id,
            date=start_local.date(),
            start_time=start_local.time(),
            end_time=end_local.time(),
            required_headcount=5,
            status=shift_status,
        )
        db.session.add(shift)
        db.session.flush()
        assignment = ShiftAssignment(
            shift_id=shift.id,
            employee_id=(employee or worker).id,
            status=status,
            token=uuid.uuid4().hex,
        )
        db.session.add(assignment)
        db.session.commit()
        return assignment

    return _make


# --------------------------------------------------------------------------- #
# Vendor email fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def vendor(app):
    v = Vendor(name="Levy", inbound_token="vendor-token-abc123", allowed_sender_domain="levy.com")
    db.session.add(v)
    db.session.commit()
    return v


def basic_auth(user: str = "sendgrid", password: str = "test-inbound-pass") -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def app():
    application = create_app(TestingConfig)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(app):
    """A test client already logged in as admin."""
    client = app.test_client()
    client.post("/login", data={"password": "test-admin-pass"})
    return client


@pytest.fixture()
def seed(app):
    """One job, one shift tomorrow (headcount 1), two employees with
    pending assignments. Returns the created objects."""
    job = Job(title="Warehouse", location_address="1 Main St", default_headcount=1)
    db.session.add(job)
    db.session.flush()

    shift = Shift(
        job_id=job.id,
        date=date.today() + timedelta(days=1),
        start_time=time(9, 0),
        end_time=time(17, 0),
        required_headcount=1,
        status=ShiftStatus.OPEN,
    )
    db.session.add(shift)

    employees = []
    assignments = []
    for i, name in enumerate(["Ana", "Ben"]):
        e = Employee(
            first_name=name,
            last_name="Test",
            phone_number=f"+1303555010{i}",
            is_active=True,
        )
        db.session.add(e)
        db.session.flush()
        a = ShiftAssignment(
            shift_id=shift.id,
            employee_id=e.id,
            status=AssignmentStatus.pending,
            token=f"tok-{name.lower()}-{'x' * 20}",
        )
        db.session.add(a)
        employees.append(e)
        assignments.append(a)

    db.session.commit()
    return {"job": job, "shift": shift, "employees": employees, "assignments": assignments}
