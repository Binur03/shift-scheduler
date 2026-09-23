"""Regression cases found by the production readiness audit."""
import pytest
from extensions import db
from models import AssignmentStatus, ShiftStatus

@pytest.mark.parametrize("status", [ShiftStatus.CANCELLED, ShiftStatus.DRAFT])
def test_closed_shift_cannot_be_accepted(client, seed, status):
    seed["shift"].status = status
    db.session.commit()
    response = client.post(f'/accept/{seed["assignments"][0].token}')
    assert response.status_code < 500
    assert seed["assignments"][0].status == AssignmentStatus.pending

@pytest.mark.parametrize("method", ["get", "post"])
def test_inactive_worker_cannot_accept(client, seed, method):
    seed["employees"][0].is_active = False
    db.session.commit()
    response = getattr(client, method)(f'/accept/{seed["assignments"][0].token}')
    assert b'Tap to confirm' not in response.data
    assert seed["assignments"][0].status == AssignmentStatus.pending

@pytest.mark.parametrize("headcount", ["abc", "0", "-1", "501"])
def test_job_rejects_invalid_headcount(admin_client, headcount):
    from models import Job
    response = admin_client.post('/admin/jobs', data={"title":"Invalid", "location_address":"Arena", "default_headcount":headcount})
    assert response.status_code == 302
    assert Job.query.count() == 0

@pytest.mark.parametrize("field,value", [("required_headcount","-1"),("required_headcount","abc"),("start_time","oops"),("job_id","oops")])
def test_shift_rejects_invalid_form(admin_client, seed, field, value):
    from models import Shift
    form = {"job_id": str(seed["job"].id), "date":"2026-12-20", "start_time":"09:00", "end_time":"17:00", "required_headcount":"2"}
    form[field] = value
    before = Shift.query.count()
    response = admin_client.post('/admin/shifts', data=form)
    assert response.status_code == 302
    assert Shift.query.count() == before

def test_paste_rejects_invalid_job_id(admin_client):
    response = admin_client.post('/admin/inbound/paste', data={"job_id":"oops", "schedule_text":"9/20 2 @ 9am"})
    assert response.status_code == 302

def test_login_rejects_backslash_redirect(app):
    from routes.auth import _safe_next
    with app.test_request_context():
        assert _safe_next('/\\evil.example') == '/admin/shifts'

def test_database_password_reserved_characters(monkeypatch):
    from config import _build_db_uri
    from sqlalchemy.engine import make_url
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DB_USER','worker')
    monkeypatch.setenv('DB_PASS','a@b:/?#%value')
    uri = _build_db_uri()
    parsed = make_url(uri) if isinstance(uri, str) else uri
    assert parsed.password == 'a@b:/?#%value'


def test_invalid_date_does_not_reuse_previous_group():
    from datetime import date
    from utils.email_parser import parse_shift_email
    result = parse_shift_email("9/20 2 @ 9am\n9/31 3 @ 10am\n4 @ 11am", today=date(2026,9,15))
    assert len(result.shifts) == 1
    assert len(result.errors) == 2


def test_offer_links_do_not_leak_via_referrer(client, seed):
    r = client.get(f'/accept/{seed["assignments"][0].token}')
    assert r.headers['Referrer-Policy'] == 'no-referrer'
    assert r.headers['Cache-Control'] == 'no-store'


def test_real_csrf_login_and_admin_form():
    import re
    from app import create_app
    from config import TestingConfig
    app = create_app(TestingConfig)
    app.config['WTF_CSRF_ENABLED'] = True
    with app.app_context():
        db.create_all()
    client = app.test_client()
    page = client.get('/login')
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)).group(1)
    assert client.post('/login', data={'password':'test-admin-pass'}).status_code == 400
    assert client.post('/login', data={'password':'test-admin-pass','csrf_token':token}).status_code == 302
    page = client.get('/admin/jobs')
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)).group(1)
    form = {'title':'CSRF audit','location_address':'Test venue','default_headcount':'2'}
    assert client.post('/admin/jobs', data=form).status_code == 400
    assert client.post('/admin/jobs', data={**form,'csrf_token':token}).status_code == 302
    from models import Job
    with app.app_context():
        assert Job.query.filter_by(title='CSRF audit').count() == 1
        db.session.remove()
        db.drop_all()
