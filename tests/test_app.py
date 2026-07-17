"""Smoke tests for the critical paths: auth gating, the accept/decline
flow (including the never-over-fill guarantee), dispatch idempotency, and
the health check."""
from __future__ import annotations

from extensions import db
from models import AssignmentStatus, ShiftAssignment, ShiftStatus


# --------------------------------------------------------------------------- #
# Health & auth
# --------------------------------------------------------------------------- #
def test_healthz_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_admin_requires_login(client):
    resp = client.get("/admin/shifts")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_wrong_password_rejected(client):
    resp = client.post("/login", data={"password": "nope"})
    assert b"Incorrect password" in resp.data
    # Still gated afterwards.
    assert client.get("/admin/shifts").status_code == 302


def test_login_success(client):
    resp = client.post("/login", data={"password": "test-admin-pass"})
    assert resp.status_code == 302
    assert client.get("/admin/shifts").status_code == 200


def test_tasks_endpoint_requires_secret(client):
    assert client.post("/tasks/check-staffing").status_code == 403
    resp = client.post(
        "/tasks/check-staffing", headers={"X-Tasks-Auth": "test-cron-secret"}
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Worker accept / decline
# --------------------------------------------------------------------------- #
def test_unknown_token_404(client):
    assert client.get("/accept/not-a-real-token").status_code == 404


def test_accept_flow(client, seed):
    ana = seed["assignments"][0]

    assert client.get(f"/accept/{ana.token}").status_code == 200

    resp = client.post(f"/accept/{ana.token}")
    assert resp.status_code == 200
    db.session.refresh(ana)
    assert ana.status == AssignmentStatus.accepted
    # Headcount 1 → shift is now filled.
    db.session.refresh(seed["shift"])
    assert seed["shift"].status == ShiftStatus.FILLED


def test_accept_is_idempotent(client, seed):
    ana = seed["assignments"][0]
    client.post(f"/accept/{ana.token}")
    # Second tap re-renders the confirmation, doesn't error or double-book.
    assert client.post(f"/accept/{ana.token}").status_code == 200
    accepted = ShiftAssignment.query.filter_by(
        status=AssignmentStatus.accepted
    ).count()
    assert accepted == 1


def test_full_shift_returns_409(client, seed):
    ana, ben = seed["assignments"]
    assert client.post(f"/accept/{ana.token}").status_code == 200
    # Seat is gone — Ben gets the "shift full" page, stays pending.
    resp = client.post(f"/accept/{ben.token}")
    assert resp.status_code == 409
    db.session.refresh(ben)
    assert ben.status == AssignmentStatus.pending


def test_decline_flow(client, seed):
    ben = seed["assignments"][1]
    resp = client.post(f"/decline/{ben.token}")
    assert resp.status_code == 200
    db.session.refresh(ben)
    assert ben.status == AssignmentStatus.rejected


def test_accepted_worker_cannot_decline(client, seed):
    ana = seed["assignments"][0]
    client.post(f"/accept/{ana.token}")
    client.post(f"/decline/{ana.token}")
    db.session.refresh(ana)
    assert ana.status == AssignmentStatus.accepted


# --------------------------------------------------------------------------- #
# Admin dispatch
# --------------------------------------------------------------------------- #
def test_dispatch_is_idempotent(admin_client, seed):
    shift = seed["shift"]
    before = ShiftAssignment.query.filter_by(shift_id=shift.id).count()

    # Dispatch twice; both employees already have assignments so no new
    # rows should appear (Twilio is unconfigured in tests → messages logged).
    admin_client.post(f"/admin/shifts/{shift.id}/dispatch")
    admin_client.post(f"/admin/shifts/{shift.id}/dispatch")

    after = ShiftAssignment.query.filter_by(shift_id=shift.id).count()
    assert after == before


def test_cancel_assignment_reopens_shift(admin_client, client, seed):
    ana = seed["assignments"][0]
    shift = seed["shift"]
    client.post(f"/accept/{ana.token}")
    db.session.refresh(shift)
    assert shift.status == ShiftStatus.FILLED

    admin_client.post(f"/admin/assignments/{ana.id}/cancel")
    db.session.refresh(shift)
    assert shift.status == ShiftStatus.OPEN
    assert shift.understaffed_alert_sent_at is None


def test_delete_employee_blocked_while_confirmed(admin_client, client, seed):
    ana_assignment = seed["assignments"][0]
    ana = seed["employees"][0]
    client.post(f"/accept/{ana_assignment.token}")

    admin_client.post(f"/admin/employees/{ana.id}/delete")
    # Still present — deletion refused while confirmed on an upcoming shift.
    from models import Employee

    assert db.session.get(Employee, ana.id) is not None
