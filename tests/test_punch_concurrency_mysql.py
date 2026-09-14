"""Concurrent Twilio webhooks against real MySQL/InnoDB row locks.

SQLite ignores SELECT ... FOR UPDATE, so these only run when a disposable
MySQL database is provided:

    TEST_MYSQL_URL="mysql+pymysql://user:pass@127.0.0.1:3306/shift_scheduler_test" \
        python -m pytest tests/test_punch_concurrency_mysql.py -v

Safety: the database name must contain "test" — every table is dropped.

Each simulated webhook runs in its own thread with its own Flask app context
(and therefore its own DB session/connection), all released simultaneously by
a barrier to maximise the collision window.
"""
from __future__ import annotations

import os
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy.engine import make_url

from conftest import SMS_PATH, sms_params, twilio_signature
from extensions import db
from models import (
    AssignmentStatus,
    Employee,
    InboundSms,
    Job,
    PunchResult,
    Shift,
    ShiftAssignment,
    ShiftStatus,
    utcnow_naive,
)
from utils.timeutil import utc_naive_to_local

MYSQL_URL = os.environ.get("TEST_MYSQL_URL")

pytestmark = pytest.mark.skipif(not MYSQL_URL, reason="set TEST_MYSQL_URL to run InnoDB concurrency tests")

THREADS = 16


@pytest.fixture(scope="module")
def mysql_app():
    if "test" not in (make_url(MYSQL_URL).database or ""):
        pytest.exit("Refusing to run: TEST_MYSQL_URL database name must contain 'test'.")

    from app import create_app
    from config import TestingConfig

    class MySQLTestingConfig(TestingConfig):
        SQLALCHEMY_DATABASE_URI = MYSQL_URL
        SQLALCHEMY_ENGINE_OPTIONS = {"pool_size": THREADS + 4, "max_overflow": 8, "pool_pre_ping": True}

    application = create_app(MySQLTestingConfig)
    with application.app_context():
        db.drop_all()
        db.create_all()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture()
def world(mysql_app):
    """Fresh rows per test: one venue job, N workers, each on a shift that is live now."""
    with mysql_app.app_context():
        for model in (InboundSms, ShiftAssignment, Shift, Employee, Job):
            db.session.query(model).delete()
        db.session.commit()

        job = Job(title="Stand 7", location_address="Arena", timezone="America/Denver")
        db.session.add(job)
        db.session.flush()
        now_local = utc_naive_to_local(utcnow_naive(), job.timezone).replace(tzinfo=None, second=0, microsecond=0)
        start = now_local - timedelta(minutes=20)
        shift = Shift(job_id=job.id, date=start.date(), start_time=start.time(),
                      end_time=(start + timedelta(hours=6)).time(),
                      required_headcount=THREADS, status=ShiftStatus.FILLED)
        db.session.add(shift)
        db.session.flush()

        phones = []
        for i in range(THREADS):
            phone = f"+1720555{i:04d}"
            employee = Employee(first_name=f"W{i}", last_name="Load", phone_number=phone)
            db.session.add(employee)
            db.session.flush()
            db.session.add(ShiftAssignment(shift_id=shift.id, employee_id=employee.id,
                                           status=AssignmentStatus.accepted, token=uuid.uuid4().hex))
            phones.append(phone)
        db.session.commit()
        return {"phones": phones, "shift_id": shift.id}


def _fire_concurrently(app, requests: list[dict]) -> list:
    """POST every signed SMS payload at the same instant; return responses in order."""
    barrier = threading.Barrier(len(requests))

    def send(params):
        headers = {"X-Twilio-Signature": twilio_signature(app, params)}
        client = app.test_client()
        barrier.wait()
        return client.post(SMS_PATH, data=params, headers=headers)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(send, requests))


def test_simultaneous_ins_from_one_worker_check_in_exactly_once(mysql_app, world):
    """16 distinct 'IN' texts from one phone at once -> 1 check-in, 15 duplicates."""
    phone = world["phones"][0]
    responses = _fire_concurrently(mysql_app, [sms_params("IN", from_=phone) for _ in range(THREADS)])

    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    with mysql_app.app_context():
        results = Counter(row.result for row in InboundSms.query.all())
        assert results == {PunchResult.CHECKED_IN: 1, PunchResult.DUPLICATE: THREADS - 1}

        employee = Employee.query.filter_by(phone_number=phone).one()
        assignment = ShiftAssignment.query.filter_by(employee_id=employee.id).one()
        assert assignment.check_in_at_utc is not None
        winner = InboundSms.query.filter_by(result=PunchResult.CHECKED_IN).one()
        assert assignment.check_in_message_sid == winner.message_sid


def test_same_message_sid_redelivered_concurrently_is_processed_once(mysql_app, world):
    """Twilio retry storm: one MessageSid delivered 16x at once -> one log row, identical replies."""
    phone = world["phones"][1]
    params = sms_params("IN", sid="SMretrystorm", from_=phone)
    responses = _fire_concurrently(mysql_app, [dict(params) for _ in range(THREADS)])

    assert all(r.status_code == 200 for r in responses)
    bodies = {r.data for r in responses}
    assert len(bodies) == 1, "every delivery of one message must get the same reply"
    assert b"Checked in" in bodies.pop()
    with mysql_app.app_context():
        assert InboundSms.query.filter_by(message_sid="SMretrystorm").count() == 1


def test_many_workers_punch_in_parallel_without_blocking_or_deadlock(mysql_app, world):
    """Shift start: every worker texts IN at once -> all succeed (no cross-worker lock contention failures)."""
    responses = _fire_concurrently(mysql_app, [sms_params("in", from_=p) for p in world["phones"]])

    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    with mysql_app.app_context():
        assert ShiftAssignment.query.filter(ShiftAssignment.check_in_at_utc.is_not(None)).count() == THREADS
        assert Counter(r.result for r in InboundSms.query.all()) == {PunchResult.CHECKED_IN: THREADS}


def test_in_and_out_racing_for_same_worker_leave_consistent_state(mysql_app, world):
    """Interleaved IN/OUT storm for one worker never produces OUT-before-IN or a 5xx."""
    phone = world["phones"][2]
    payloads = [sms_params("IN" if i % 2 == 0 else "OUT", from_=phone) for i in range(THREADS)]
    responses = _fire_concurrently(mysql_app, payloads)

    assert all(r.status_code == 200 for r in responses)
    with mysql_app.app_context():
        employee = Employee.query.filter_by(phone_number=phone).one()
        assignment = ShiftAssignment.query.filter_by(employee_id=employee.id).one()
        results = Counter(r.result for r in InboundSms.query.filter_by(employee_id=employee.id))
        # Exactly one real IN; at most one real OUT, and only if it came after the IN.
        assert results[PunchResult.CHECKED_IN] == 1
        assert results[PunchResult.CHECKED_OUT] <= 1
        if assignment.check_out_at_utc is not None:
            assert assignment.check_out_at_utc >= assignment.check_in_at_utc
        assert sum(results.values()) == THREADS


def _web_setup(mysql_app, world, phone_index: int, pin: str = "4821") -> tuple[str, int]:
    """Give one worker a PIN and their assignment a punch token."""
    from utils.pins import hash_pin
    from utils.web_punch import ensure_punch_token

    with mysql_app.app_context():
        employee = Employee.query.filter_by(phone_number=world["phones"][phone_index]).one()
        employee.pin_hash = hash_pin(employee.id, pin)
        assignment = ShiftAssignment.query.filter_by(employee_id=employee.id).one()
        token = ensure_punch_token(assignment)
        db.session.commit()
        return token, assignment.id


def _fire_json_concurrently(app, payloads: list[dict]) -> list:
    barrier = threading.Barrier(len(payloads))

    def send(payload):
        client = app.test_client()
        barrier.wait()
        return client.post("/api/punch", json=payload)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(send, payloads))


def test_web_pin_double_taps_check_in_exactly_once(mysql_app, world):
    """16 simultaneous correct-PIN 'in' taps -> 1 checked_in, 15 already_checked_in, one timestamp."""
    token, assignment_id = _web_setup(mysql_app, world, phone_index=3)
    responses = _fire_json_concurrently(
        mysql_app, [{"token": token, "pin": "4821", "action": "in"} for _ in range(THREADS)]
    )
    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    statuses = Counter(r.get_json()["status"] for r in responses)
    assert statuses == {"checked_in": 1, "already_checked_in": THREADS - 1}
    assert len({r.get_json()["time"] for r in responses}) == 1  # everyone sees the one real timestamp


def test_parallel_wrong_pins_cannot_exceed_lockout(mysql_app, world):
    """16 simultaneous wrong guesses: the attempt counter is updated under the row
    lock, so exactly 4 get 'wrong_pin' and the 5th locks — no lost updates."""
    from utils.web_punch import MAX_PIN_ATTEMPTS

    token, assignment_id = _web_setup(mysql_app, world, phone_index=4)
    responses = _fire_json_concurrently(
        mysql_app, [{"token": token, "pin": "0001", "action": "in"} for _ in range(THREADS)]
    )
    codes = Counter(r.status_code for r in responses)
    assert codes == {401: MAX_PIN_ATTEMPTS - 1, 423: THREADS - (MAX_PIN_ATTEMPTS - 1)}
    with mysql_app.app_context():
        assignment = db.session.get(ShiftAssignment, assignment_id)
        assert assignment.pin_locked_until_utc is not None
        assert assignment.check_in_at_utc is None


def test_sms_and_web_racing_for_same_shift_check_in_once(mysql_app, world):
    """8 SMS 'IN' + 8 keypad 'in' for one worker at once: one timestamp, never overwritten."""
    token, assignment_id = _web_setup(mysql_app, world, phone_index=5)
    phone = world["phones"][5]
    barrier = threading.Barrier(16)

    def sms():
        params = sms_params("IN", from_=phone)
        headers = {"X-Twilio-Signature": twilio_signature(mysql_app, params)}
        client = mysql_app.test_client()
        barrier.wait()
        return client.post(SMS_PATH, data=params, headers=headers).status_code

    def web():
        client = mysql_app.test_client()
        barrier.wait()
        return client.post("/api/punch", json={"token": token, "pin": "4821", "action": "in"}).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(sms) for _ in range(8)] + [pool.submit(web) for _ in range(8)]
        codes = [f.result() for f in futures]

    assert all(code == 200 for code in codes), codes
    with mysql_app.app_context():
        assignment = db.session.get(ShiftAssignment, assignment_id)
        assert assignment.check_in_at_utc is not None
        sms_wins = InboundSms.query.filter_by(result=PunchResult.CHECKED_IN, employee_id=assignment.employee_id).count()
        assert (sms_wins == 1) == (assignment.check_in_source == "sms")
        assert sms_wins <= 1


def test_out_storm_across_workers_survives_gap_lock_deadlocks(mysql_app, world):
    """All workers check in, then all check out simultaneously; deadlock retries keep every OUT."""
    _fire_concurrently(mysql_app, [sms_params("IN", from_=p) for p in world["phones"]])
    responses = _fire_concurrently(mysql_app, [sms_params("OUT", from_=p) for p in world["phones"]])

    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    with mysql_app.app_context():
        assert ShiftAssignment.query.filter(ShiftAssignment.check_out_at_utc.is_not(None)).count() == THREADS
