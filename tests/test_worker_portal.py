"""The weekly portal: one link per worker instead of one text per shift.

A ten-shift week across twenty workers was two hundred messages. This is the
same first-come-first-served claim, reached from a single page.
"""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from extensions import db
from models import (
    AssignmentStatus,
    Employee,
    Shift,
    ShiftAssignment,
    ShiftStatus,
    utcnow_naive,
)
from utils.web_punch import ensure_portal_token


@pytest.fixture()
def crew(app, venue_job):
    """Two workers and three future shifts, everyone invited to all of them."""
    workers = []
    for first, last, phone, lang in [
        ("Maria", "Gonzalez", "+17205550101", "es"),
        ("Carmen", "Ortiz", "+17205550103", "en"),
    ]:
        w = Employee(first_name=first, last_name=last, phone_number=phone, language=lang)
        db.session.add(w)
        workers.append(w)
    db.session.flush()

    shifts = []
    for offset, start, headcount in [(2, time(13, 0), 2), (3, time(6, 0), 1), (4, time(15, 0), 2)]:
        sh = Shift(job_id=venue_job.id, date=date.today() + timedelta(days=offset),
                   start_time=start, required_headcount=headcount, status=ShiftStatus.OPEN)
        db.session.add(sh)
        shifts.append(sh)
    db.session.flush()

    for sh in shifts:
        for i, w in enumerate(workers):
            db.session.add(ShiftAssignment(
                shift_id=sh.id, employee_id=w.id,
                status=AssignmentStatus.pending, token=f"tok-{sh.id}-{w.id}",
            ))
    for w in workers:
        ensure_portal_token(w)
    db.session.commit()
    return {"workers": workers, "shifts": shifts}


class TestPortalPage:
    def test_lists_every_offer_grouped_by_day(self, client, crew):
        worker = crew["workers"][0]
        page = client.get(f"/my/{worker.portal_token}").get_data(as_text=True)

        assert "Elija los días que puede trabajar" in page  # her language
        for shift in crew["shifts"]:
            assert f"tok-{shift.id}-{worker.id}" in page

    def test_unknown_token_is_404(self, client, crew):
        assert client.get("/my/not-a-real-token").status_code == 404

    def test_an_inactive_worker_gets_nothing(self, client, crew):
        worker = crew["workers"][0]
        worker.is_active = False
        db.session.commit()
        assert client.get(f"/my/{worker.portal_token}").status_code == 404

    def test_past_shifts_are_not_shown(self, client, crew, venue_job):
        """Opening the link on Friday should not mean scrolling past Monday."""
        worker = crew["workers"][0]
        old = Shift(job_id=venue_job.id, date=date.today() - timedelta(days=3),
                    start_time=time(9, 0), required_headcount=1, status=ShiftStatus.OPEN)
        db.session.add(old)
        db.session.flush()
        db.session.add(ShiftAssignment(shift_id=old.id, employee_id=worker.id,
                                       status=AssignmentStatus.pending, token="tok-old"))
        db.session.commit()

        page = client.get(f"/my/{worker.portal_token}").get_data(as_text=True)
        assert "tok-old" not in page

    def test_cancelled_shifts_disappear(self, client, crew):
        worker = crew["workers"][0]
        gone = crew["shifts"][0]
        gone.status = ShiftStatus.CANCELLED
        db.session.commit()

        page = client.get(f"/my/{worker.portal_token}").get_data(as_text=True)
        assert f"tok-{gone.id}-{worker.id}" not in page


class TestClaimingFromThePortal:
    def test_accepting_takes_the_seat(self, client, crew):
        worker, shift = crew["workers"][0], crew["shifts"][0]
        token = f"tok-{shift.id}-{worker.id}"

        response = client.post(f"/my/{worker.portal_token}/accept/{token}")

        assert response.status_code == 302
        assert "just=accepted" in response.headers["Location"]
        assert ShiftAssignment.query.filter_by(token=token).one().status == (
            AssignmentStatus.accepted
        )

    def test_declining_records_it(self, client, crew):
        worker, shift = crew["workers"][0], crew["shifts"][0]
        token = f"tok-{shift.id}-{worker.id}"

        response = client.post(f"/my/{worker.portal_token}/decline/{token}")

        assert "just=declined" in response.headers["Location"]
        assert ShiftAssignment.query.filter_by(token=token).one().status == (
            AssignmentStatus.rejected
        )

    def test_a_full_shift_says_so(self, client, crew):
        """Single-seat shift already taken by the other worker."""
        maria, carmen = crew["workers"]
        shift = crew["shifts"][1]            # headcount 1
        ShiftAssignment.query.filter_by(token=f"tok-{shift.id}-{carmen.id}").one().status = (
            AssignmentStatus.accepted
        )
        db.session.commit()

        response = client.post(
            f"/my/{maria.portal_token}/accept/tok-{shift.id}-{maria.id}"
        )
        assert "just=full" in response.headers["Location"]
        assert ShiftAssignment.query.filter_by(
            token=f"tok-{shift.id}-{maria.id}").one().status == AssignmentStatus.pending

    def test_one_worker_cannot_answer_for_another(self, client, crew):
        """The portal token must not become a way to claim someone else's seat."""
        maria, carmen = crew["workers"]
        shift = crew["shifts"][0]
        carmens_offer = f"tok-{shift.id}-{carmen.id}"

        response = client.post(f"/my/{maria.portal_token}/accept/{carmens_offer}")

        assert response.status_code == 404
        assert ShiftAssignment.query.filter_by(token=carmens_offer).one().status == (
            AssignmentStatus.pending
        )

    def test_accepting_twice_is_harmless(self, client, crew):
        worker, shift = crew["workers"][0], crew["shifts"][0]
        token = f"tok-{shift.id}-{worker.id}"

        client.post(f"/my/{worker.portal_token}/accept/{token}")
        response = client.post(f"/my/{worker.portal_token}/accept/{token}")

        assert "just=accepted" in response.headers["Location"]
        assert ShiftAssignment.query.filter_by(
            shift_id=shift.id, status=AssignmentStatus.accepted).count() == 1


class TestWeekDigest:
    @pytest.fixture()
    def sent(self, monkeypatch):
        from utils import sms as sms_module

        out: list[dict] = []
        monkeypatch.setattr(sms_module.WhatsAppService, "_send",
                            lambda self, to, **kw: out.append({"to": to, **kw}) or True)
        monkeypatch.setenv("BUSINESS_NAME", "Rohan Binu")
        return out

    def test_one_message_per_worker_not_one_per_shift(self, admin_client, crew, sent):
        """Three shifts, two workers: two texts, not six."""
        week = (date.today() + timedelta(days=2)).isoformat()
        admin_client.post("/admin/shifts/dispatch-week", data={"week_start": week})

        # Matched on the portal link rather than wording, so rephrasing the
        # copy cannot quietly break this.
        digests = [m for m in sent if "/my/" in (m.get("body") or "")]
        assert len(digests) == 2, f"expected 2 digests, got {len(digests)}"
        assert {m["to"] for m in digests} == {"+17205550101", "+17205550103"}

    def test_the_digest_links_to_the_workers_own_portal(self, admin_client, crew, sent):
        week = (date.today() + timedelta(days=2)).isoformat()
        admin_client.post("/admin/shifts/dispatch-week", data={"week_start": week})

        for worker in crew["workers"]:
            db.session.refresh(worker)
            mine = [m for m in sent if m["to"] == worker.phone_number]
            assert mine, f"nothing sent to {worker.full_name}"
            assert f"/my/{worker.portal_token}" in mine[0]["body"]

    def test_each_worker_is_written_to_in_their_own_language(
        self, admin_client, crew, sent
    ):
        week = (date.today() + timedelta(days=2)).isoformat()
        admin_client.post("/admin/shifts/dispatch-week", data={"week_start": week})

        spanish = [m for m in sent if m["to"] == "+17205550101"][0]["body"]
        english = [m for m in sent if m["to"] == "+17205550103"][0]["body"]
        assert "turnos nuevos disponibles" in spanish
        assert "new shifts available" in english

    def test_the_digest_is_one_or_two_segments(self, admin_client, crew, sent):
        """The whole point is that it does not grow with the number of shifts."""
        from utils.gsm7 import segment_count, to_gsm7

        week = (date.today() + timedelta(days=2)).isoformat()
        admin_client.post("/admin/shifts/dispatch-week", data={"week_start": week})

        for message in sent:
            assert segment_count(to_gsm7(message["body"])) <= 2
