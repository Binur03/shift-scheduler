"""Vendor email webhook: authentication, DKIM enforcement, ingestion, approval."""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from conftest import basic_auth
from extensions import db
from models import (
    InboundEmail,
    Job,
    ParseStatus,
    Shift,
    ShiftAssignment,
    ShiftSource,
    ShiftStatus,
)

URL = "/webhooks/email/vendor-token-abc123"


def email_form(body: str, *, message_id: str = "<m1@levy.com>", dkim: str = "{@levy.com : pass}",
               sender: str = "Ops Team <ops@levy.com>") -> dict:
    """Fields shaped like a SendGrid Inbound Parse POST."""
    return {
        "from": sender,
        "to": "shifts@inbound.example.com",
        "subject": "Staffing request",
        "text": body,
        "headers": f"Message-ID: {message_id}\nSubject: Staffing request\n",
        "dkim": dkim,
        "SPF": "pass",
    }


@pytest.fixture()
def stand(app):
    job = Job(title="Concessions Stand 12", location_address="Ball Arena", timezone="America/Denver")
    db.session.add(job)
    db.session.commit()
    return job


def shift_line(days_out: int = 5, position: str = "Concessions Stand 12", count: int = 6) -> str:
    d = date.today() + timedelta(days=days_out)
    return f"{d:%m/%d/%Y} | 4:00 PM - 11:00 PM | {position} | {count}"


class TestAuthentication:
    def test_missing_basic_auth_is_401_with_challenge(self, client, vendor):
        response = client.post(URL, data=email_form(shift_line()))
        assert response.status_code == 401
        assert "Basic" in response.headers["WWW-Authenticate"]
        assert InboundEmail.query.count() == 0

    @pytest.mark.parametrize("user,password", [
        ("sendgrid", "wrong"), ("wrong", "test-inbound-pass"), ("", ""),
    ])
    def test_wrong_basic_auth_is_401(self, client, vendor, user, password):
        response = client.post(URL, data=email_form(shift_line()), headers=basic_auth(user, password))
        assert response.status_code == 401

    def test_unknown_vendor_token_is_404(self, client, vendor):
        response = client.post("/webhooks/email/not-a-token", data=email_form(shift_line()), headers=basic_auth())
        assert response.status_code == 404

    def test_disabled_vendor_is_404(self, client, vendor):
        vendor.is_active = False
        db.session.commit()
        assert client.post(URL, data=email_form(shift_line()), headers=basic_auth()).status_code == 404

    def test_unconfigured_credentials_fail_closed(self, client, vendor, monkeypatch):
        monkeypatch.delenv("INBOUND_EMAIL_PASSWORD")
        assert client.post(URL, data=email_form(shift_line()), headers=basic_auth()).status_code == 503


class TestSenderVerification:
    @pytest.mark.parametrize("dkim,sender", [
        ("{@levy.com : fail}", "ops@levy.com"),                    # DKIM failed
        ("{@evil.com : pass}", "ops@levy.com"),                    # signed by another domain
        ("", "ops@levy.com"),                                      # unsigned
        ("{@levy.com : pass}", "ops@levy.com.evil.net"),           # lookalike From domain
        ("{@levy.com : pass}", "attacker@evil.com"),               # DKIM ok but From spoofed
        ("{@notlevy.com : pass}", "ops@notlevy.com"),              # suffix trick
    ])
    def test_unverified_sender_rejected_without_shifts(self, client, vendor, stand, dkim, sender):
        response = client.post(URL, data=email_form(shift_line(), dkim=dkim, sender=sender), headers=basic_auth())
        assert response.status_code == 200  # non-transient: don't make SendGrid retry
        assert response.get_json()["status"] == ParseStatus.REJECTED
        assert Shift.query.count() == 0
        email = InboundEmail.query.one()
        assert email.raw_body  # still stored for audit

    def test_subdomain_of_vendor_domain_accepted(self, client, vendor, stand):
        form = email_form(shift_line(), dkim="{@mail.levy.com : pass}", sender="ops@mail.levy.com")
        assert client.post(URL, data=form, headers=basic_auth()).get_json()["status"] == ParseStatus.PARSED


class TestIngestion:
    def test_valid_email_creates_draft_shifts(self, client, vendor, stand):
        body = "Hi,\n" + shift_line(5) + "\n" + shift_line(6, count=3) + "\nThanks"
        payload = client.post(URL, data=email_form(body), headers=basic_auth()).get_json()

        assert payload["status"] == ParseStatus.PARSED and payload["shifts_created"] == 2
        shifts = Shift.query.order_by(Shift.date).all()
        assert [s.required_headcount for s in shifts] == [6, 3]
        assert all(s.status == ShiftStatus.DRAFT for s in shifts)
        assert all(s.source == ShiftSource.EMAIL and s.vendor_id == vendor.id for s in shifts)
        assert shifts[0].start_time == time(16) and shifts[0].end_time == time(23)

    def test_unknown_position_reported_partial(self, client, vendor, stand):
        body = shift_line(5) + "\n" + shift_line(5, position="Suite Bartender")
        payload = client.post(URL, data=email_form(body), headers=basic_auth()).get_json()
        assert payload["status"] == ParseStatus.PARTIAL and payload["shifts_created"] == 1
        (error,) = InboundEmail.query.one().errors
        assert "no job named 'Suite Bartender'" in error["error"] and error["line"] == 2

    def test_position_match_is_case_insensitive(self, client, vendor, stand):
        payload = client.post(URL, data=email_form(shift_line(position="concessions STAND 12")),
                              headers=basic_auth()).get_json()
        assert payload["shifts_created"] == 1

    def test_nothing_usable_is_failed(self, client, vendor, stand):
        payload = client.post(URL, data=email_form("Can you staff the game Saturday? Thanks!"),
                              headers=basic_auth()).get_json()
        assert payload["status"] == ParseStatus.FAILED and payload["shifts_created"] == 0

    def test_redelivered_message_id_is_noop(self, client, vendor, stand):
        form = email_form(shift_line())
        first = client.post(URL, data=form, headers=basic_auth()).get_json()
        again = client.post(URL, data=form, headers=basic_auth()).get_json()
        assert again["duplicate"] is True and again["id"] == first["id"]
        assert Shift.query.count() == 1 and InboundEmail.query.count() == 1

    def test_missing_message_id_deduped_by_content_hash(self, client, vendor, stand):
        form = email_form(shift_line())
        form["headers"] = "Subject: Staffing request\n"
        client.post(URL, data=form, headers=basic_auth())
        client.post(URL, data=form, headers=basic_auth())
        assert InboundEmail.query.count() == 1

    def test_same_shift_in_new_email_not_doubled(self, client, vendor, stand):
        client.post(URL, data=email_form(shift_line(), message_id="<a@levy.com>"), headers=basic_auth())
        payload = client.post(URL, data=email_form(shift_line(), message_id="<b@levy.com>"),
                              headers=basic_auth()).get_json()
        assert payload["shifts_created"] == 0
        assert Shift.query.count() == 1
        assert "already scheduled" in InboundEmail.query.filter_by(message_id="<b@levy.com>").one().errors[0]["error"]

    def test_html_only_email(self, client, vendor, stand):
        d = date.today() + timedelta(days=5)
        form = email_form("")
        form["html"] = (f"<table><tr><td>{d:%m/%d/%Y}</td><td>4pm - 11pm</td>"
                        "<td>Concessions Stand 12</td><td>6</td></tr></table>")
        assert client.post(URL, data=form, headers=basic_auth()).get_json()["shifts_created"] == 1


class TestApprovalFlow:
    def _ingest(self, client):
        body = shift_line(5) + "\n" + shift_line(6)
        return client.post(URL, data=email_form(body), headers=basic_auth()).get_json()["id"]

    def test_drafts_cannot_be_dispatched_or_copied(self, client, admin_client, vendor, stand, seed):
        self._ingest(client)
        draft = Shift.query.filter_by(status=ShiftStatus.DRAFT).first()
        admin_client.post(f"/admin/shifts/{draft.id}/dispatch")
        assert ShiftAssignment.query.filter_by(shift_id=draft.id).count() == 0

        admin_client.post("/admin/shifts/dispatch-week", data={"week_start": date.today().isoformat()})
        assert ShiftAssignment.query.filter_by(shift_id=draft.id).count() == 0

    def test_approve_publishes_drafts(self, client, admin_client, vendor, stand):
        email_id = self._ingest(client)
        admin_client.post(f"/admin/inbound/{email_id}/approve")
        assert Shift.query.filter_by(status=ShiftStatus.DRAFT).count() == 0
        assert Shift.query.filter_by(status=ShiftStatus.OPEN).count() == 2

    def test_discard_deletes_only_drafts(self, client, admin_client, vendor, stand):
        email_id = self._ingest(client)
        first = Shift.query.filter_by(status=ShiftStatus.DRAFT).order_by(Shift.date).first()
        first.status = ShiftStatus.OPEN  # one already approved
        db.session.commit()
        admin_client.post(f"/admin/inbound/{email_id}/discard")
        assert Shift.query.count() == 1 and Shift.query.one().status == ShiftStatus.OPEN

    def test_inbox_requires_login(self, client):
        assert client.get("/admin/inbound").status_code == 302

    def test_inbox_renders_errors_and_escapes_email_content(self, client, admin_client, vendor, stand):
        body = shift_line(5) + "\n<script>alert(1)</script> 09/20/2026 | 4pm-11pm | <b>X</b>"
        client.post(URL, data=email_form(body), headers=basic_auth())
        page = admin_client.get("/admin/inbound").data
        assert b"<script>alert(1)</script>" not in page  # autoescaped
        assert b"&lt;script&gt;" in page

    def test_create_vendor_generates_token(self, admin_client):
        from models import Vendor

        admin_client.post("/admin/vendors", data={"name": "Aramark", "allowed_sender_domain": "@Aramark.com"})
        created = Vendor.query.filter_by(name="Aramark").one()
        assert created.allowed_sender_domain == "aramark.com"
        assert len(created.inbound_token) >= 40
