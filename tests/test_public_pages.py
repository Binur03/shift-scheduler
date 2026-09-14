"""Public business / SMS-policy pages used for carrier and toll-free verification."""
from __future__ import annotations

import pytest

PAGES = ["/", "/sms-terms", "/privacy"]


@pytest.fixture()
def business(monkeypatch):
    monkeypatch.setenv("BUSINESS_NAME", "Example Arena Cleaning LLC")
    monkeypatch.setenv("BUSINESS_LOCATION", "Littleton, Colorado")
    monkeypatch.setenv("BUSINESS_CONTACT_EMAIL", "shifts@example.com")
    monkeypatch.setenv("TWILIO_SMS_NUMBER", "+18447135873")
    monkeypatch.setenv("POLICY_EFFECTIVE_DATE", "September 14, 2026")
    monkeypatch.delenv("BUSINESS_ADDRESS", raising=False)
    monkeypatch.delenv("BUSINESS_CONTACT_PHONE", raising=False)


class TestUnconfigured:
    """Without a real business name nothing placeholder-y is ever published."""

    def test_home_still_redirects_to_admin(self, client, monkeypatch):
        monkeypatch.delenv("BUSINESS_NAME", raising=False)
        response = client.get("/")
        assert response.status_code == 302
        assert "/admin/shifts" in response.headers["Location"]

    @pytest.mark.parametrize("path", ["/sms-terms", "/privacy"])
    def test_policy_pages_404(self, client, monkeypatch, path):
        monkeypatch.delenv("BUSINESS_NAME", raising=False)
        assert client.get(path).status_code == 404

    def test_blank_name_counts_as_unconfigured(self, client, monkeypatch):
        monkeypatch.setenv("BUSINESS_NAME", "   ")
        assert client.get("/").status_code == 302


class TestConfigured:
    @pytest.mark.parametrize("path", PAGES)
    def test_publicly_accessible_without_login(self, client, business, path):
        response = client.get(path)
        assert response.status_code == 200  # reviewers must reach it with no session
        page = response.data.decode()
        assert "Example Arena Cleaning LLC" in page
        assert "Staff sign-in" in page

    def test_home_describes_program_and_opt_out(self, client, business):
        page = client.get("/").data.decode()
        assert "(844) 713-5873" in page
        assert "STOP" in page and "HELP" in page
        assert "Message and data rates may apply" in page
        assert "/sms-terms" in page and "/privacy" in page

    def test_sms_terms_has_carrier_required_disclosures(self, client, business):
        page = client.get("/sms-terms").data.decode()
        for required in [
            "Example Arena Cleaning LLC Shift Alerts",   # program name
            "opt in",                                     # how consent is collected
            "STOP", "START", "HELP",                      # opt-out / help keywords
            "Message frequency varies",                   # frequency
            "Message and data rates may apply",           # cost
            "Carriers are not liable",                    # carrier disclaimer
            "September 14, 2026",                         # effective date
        ]:
            assert required in page, required

    def test_privacy_has_no_sharing_statement(self, client, business):
        page = client.get("/privacy").data.decode()
        assert "do not sell, rent, or share mobile phone numbers" in page
        assert "Twilio" in page and "Google Cloud" in page

    def test_optional_contact_fields_only_when_set(self, client, business, monkeypatch):
        page = client.get("/").data.decode()
        assert "Phone:" not in page
        monkeypatch.setenv("BUSINESS_CONTACT_PHONE", "+13035550100")
        monkeypatch.setenv("BUSINESS_ADDRESS", "123 Example St, Littleton, CO 80129")
        page = client.get("/").data.decode()
        assert "(303) 555-0100" in page and "123 Example St" in page

    def test_business_values_are_escaped(self, client, business, monkeypatch):
        monkeypatch.setenv("BUSINESS_NAME", "<script>alert(1)</script>")
        page = client.get("/sms-terms").data.decode()
        assert "<script>alert(1)</script>" not in page

    def test_admin_still_requires_login(self, client, business):
        assert client.get("/admin/shifts").status_code == 302
