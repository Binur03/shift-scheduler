"""Workflow tests: confirmed workers can find their PIN screen safely."""
import pytest
from extensions import db
from models import AssignmentStatus, ShiftStatus


def test_confirmation_opens_keypad_without_punching(client, seed):
    a = seed['assignments'][0]
    a.status = AssignmentStatus.accepted
    a.punch_token = None  # worker accepted before keypad links were introduced
    db.session.commit()
    page = client.get(f'/accept/{a.token}')
    assert b'Open check-in' in page.data
    response = client.post(f'/accept/{a.token}/check-in')
    assert response.status_code == 302
    assert response.headers['Location'] == f'/punch/{a.punch_token}'
    token = a.punch_token
    assert token
    assert a.check_in_at_utc is None and a.check_out_at_utc is None
    client.post(f'/accept/{a.token}/check-in')
    assert a.punch_token == token
    assert client.get(response.headers['Location']).status_code == 200


@pytest.mark.parametrize('condition', ['pending','cancelled','inactive','draft'])
def test_unavailable_offers_cannot_open_keypad(client, seed, condition):
    a = seed['assignments'][0]
    a.status = AssignmentStatus.accepted
    a.punch_token = None
    if condition == 'pending':
        a.status = AssignmentStatus.pending
    elif condition == 'cancelled':
        a.status = AssignmentStatus.cancelled
    elif condition == 'inactive':
        a.employee.is_active = False
    else:
        a.shift.status = ShiftStatus.DRAFT
    db.session.commit()
    response = client.post(f'/accept/{a.token}/check-in')
    assert response.status_code == 409
    assert a.punch_token is None
    assert a.check_in_at_utc is None


def test_manager_pages_render_with_mixed_shift_states(admin_client, seed):
    for state in [ShiftStatus.DRAFT, ShiftStatus.CANCELLED, ShiftStatus.OPEN]:
        seed['shift'].status = state
        db.session.commit()
        r = admin_client.get('/admin/shifts')
        assert r.status_code == 200
        assert r.data.index(b'data-shift-row') < r.data.index(b'id="create-shifts"')
        detail = admin_client.get(f'/admin/shifts/{seed["shift"].id}')
        assert detail.status_code == 200
        send_action = f'/admin/shifts/{seed["shift"].id}/dispatch'.encode()
        assert (send_action in detail.data) == (state == ShiftStatus.OPEN)
