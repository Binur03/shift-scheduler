"""Shared pytest fixtures: an app bound to an in-memory SQLite database."""
from __future__ import annotations

import os
import sys
from datetime import date, time, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")
os.environ.setdefault("CRON_SECRET", "test-cron-secret")

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
)


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
