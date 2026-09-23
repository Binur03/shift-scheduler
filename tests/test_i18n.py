"""English/Spanish support: language resolution, the toggle, and localized output.

Covers the three ways a language gets chosen (URL, cookie, the worker's own
record), that a worker's text messages follow their record rather than the
manager's console, and that the catalog itself stays consistent as strings are
added.
"""
from __future__ import annotations

import re
from datetime import date, time, timedelta

import pytest

from extensions import db
from models import AssignmentStatus, Employee, ShiftAssignment
from utils.i18n import (
    CATALOG,
    SUPPORTED_LANGUAGES,
    format_date_long,
    format_date_short,
    normalize,
    translate,
)


# One definition of "a t() call in a template", shared by the catalog checks.
# The backreference keeps the closing quote matched to the opening one.
TEMPLATE_T_CALL = r"""(?<![\w.])t\(\s*(["'])(.*?)\1"""


# --------------------------------------------------------------------------- #
# Language resolution
# --------------------------------------------------------------------------- #
class TestLanguageResolution:
    def test_defaults_to_english(self, client, make_assignment):
        assignment = make_assignment(status=AssignmentStatus.pending)
        page = client.get(f"/accept/{assignment.token}").get_data(as_text=True)
        # Jinja escapes the apostrophe in "You're", so match a plain fragment.
        assert "invited to a shift" in page
        assert 'lang="en"' in page

    def test_query_parameter_switches_language(self, client, make_assignment):
        assignment = make_assignment(status=AssignmentStatus.pending)
        page = client.get(f"/accept/{assignment.token}?lang=es").get_data(as_text=True)
        assert "Le invitamos a un turno" in page
        assert 'lang="es"' in page

    def test_toggle_sets_a_cookie_that_persists(self, client, make_assignment):
        assignment = make_assignment(status=AssignmentStatus.pending)
        target = f"/accept/{assignment.token}"

        redirect = client.get(f"/language/es?next={target}")
        assert redirect.status_code == 302
        assert redirect.headers["Location"].endswith(target)

        # The next request carries no ?lang= and must still be Spanish.
        page = client.get(target).get_data(as_text=True)
        assert "Le invitamos a un turno" in page

    def test_unknown_language_is_ignored(self, client, make_assignment):
        assignment = make_assignment(status=AssignmentStatus.pending)
        page = client.get(f"/accept/{assignment.token}?lang=fr").get_data(as_text=True)
        assert "invited to a shift" in page

    @pytest.mark.parametrize(
        "raw,expected",
        [("es", "es"), ("ES", "es"), ("es-MX", "es"), ("es_419", "es"),
         ("en-US", "en"), ("fr", None), ("", None), (None, None), (123, None)],
    )
    def test_normalize(self, raw, expected):
        assert normalize(raw) == expected

    def test_browser_accept_language_is_used_when_nothing_chosen(self, client):
        """Applies where no worker record overrides it — e.g. the login page."""
        page = client.get(
            "/login", headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
        ).get_data(as_text=True)
        assert "Contraseña de administrador" in page

    def test_saved_worker_language_outranks_the_browser_header(
        self, client, make_assignment, worker
    ):
        """The roster is a deliberate choice; a borrowed phone's locale is not."""
        worker.language = "en"
        db.session.commit()
        assignment = make_assignment(status=AssignmentStatus.pending)
        page = client.get(
            f"/accept/{assignment.token}",
            headers={"Accept-Language": "es-MX,es;q=0.9"},
        ).get_data(as_text=True)
        assert "invited to a shift" in page


class TestWorkerSavedLanguage:
    def test_worker_pages_open_in_the_workers_language(self, client, make_assignment, worker):
        worker.language = "es"
        db.session.commit()
        assignment = make_assignment(status=AssignmentStatus.pending)
        page = client.get(f"/accept/{assignment.token}").get_data(as_text=True)
        assert "Le invitamos a un turno" in page

    def test_an_explicit_choice_beats_the_saved_language(
        self, client, make_assignment, worker
    ):
        """A shared phone toggled to English must stay English."""
        worker.language = "es"
        db.session.commit()
        assignment = make_assignment(status=AssignmentStatus.pending)

        client.get(f"/language/en?next=/accept/{assignment.token}")
        page = client.get(f"/accept/{assignment.token}").get_data(as_text=True)
        assert "invited to a shift" in page
        assert "Le invitamos a un turno" not in page

    def test_punch_keypad_follows_the_worker(self, client, make_assignment, worker):
        worker.language = "es"
        db.session.commit()
        assignment = make_assignment(start_in=timedelta(minutes=-5))
        assignment.punch_token = "punchtok123"
        db.session.commit()

        page = client.get("/punch/punchtok123").get_data(as_text=True)
        assert "Escriba su PIN" in page
        assert "PIN INCORRECTO" in page  # the JS string table is rendered too


# --------------------------------------------------------------------------- #
# The toggle must not become an open redirect
# --------------------------------------------------------------------------- #
class TestToggleRedirectSafety:
    @pytest.mark.parametrize(
        "evil",
        ["//evil.example", "https://evil.example", "/\\evil.example",
         "%2f%2fevil.example", "http://evil.example/x"],
    )
    def test_offsite_next_is_refused(self, client, evil):
        response = client.get(f"/language/es?next={evil}")
        assert response.status_code == 302
        assert "evil.example" not in response.headers["Location"]

    def test_same_site_next_is_kept(self, client):
        response = client.get("/language/es?next=/admin/shifts")
        assert response.headers["Location"].endswith("/admin/shifts")

    def test_lang_is_stripped_from_the_return_target(self, client, make_assignment):
        """Otherwise ?lang= would out-rank the cookie and bounce straight back."""
        assignment = make_assignment(status=AssignmentStatus.pending)
        page = client.get(f"/accept/{assignment.token}?lang=es").get_data(as_text=True)
        toggle = re.search(r'href="(/language/en\?next=[^"]+)"', page)
        assert toggle, "language toggle link not rendered"
        assert "lang%3Den" not in toggle.group(1)
        assert "lang=" not in toggle.group(1).split("next=")[1]


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
class TestLocalizedDates:
    def test_spanish_long_date(self):
        assert format_date_long(date(2026, 9, 18), lang="es") == (
            "viernes, 18 de septiembre de 2026"
        )

    def test_english_long_date(self):
        assert format_date_long(date(2026, 9, 18), lang="en") == "Friday, 18 September 2026"

    def test_short_date_with_year_matches_the_original_sms_format(self):
        assert format_date_short(date(2026, 9, 18), lang="en", year=True) == "Fri 18 Sep 2026"

    def test_none_is_empty_not_an_error(self):
        assert format_date_long(None, lang="es") == ""


# --------------------------------------------------------------------------- #
# SMS goes out in the worker's language, not the manager's
# --------------------------------------------------------------------------- #
class TestLocalizedSms:
    def _details(self):
        from utils.sms import ShiftBroadcastDetails

        return ShiftBroadcastDetails(
            title="Ball Arena",
            location_address="1000 Chopper Cir",
            work_date=date(2026, 9, 18),
            start_time=time(15, 0),
            end_time=None,
            estimated_hours=None,
            area="Parking",
        )

    def test_spanish_invitation_body(self, app):
        from utils.sms import WhatsAppService

        body = WhatsAppService()._format_body(self._details(), "TOK", "es")
        assert "Nuevo turno disponible: Ball Arena" in body
        assert "Fecha: vie 18 sep 2026" in body
        assert "Acepte (por orden de llegada):" in body
        assert "/accept/TOK" in body

    def test_stop_line_is_spanish_on_sms_and_absent_on_whatsapp(self, app):
        """SMS must carry an opt-out line; WhatsApp has its own block controls."""
        from utils.sms import WhatsAppService

        sms = WhatsAppService(channel="sms")._format_body(self._details(), "TOK", "es")
        assert "Responda STOP para cancelar." in sms

        wa = WhatsAppService(channel="whatsapp")._format_body(self._details(), "TOK", "es")
        assert "STOP" not in wa

    def test_english_invitation_body_is_unchanged(self, app):
        from utils.sms import WhatsAppService

        body = WhatsAppService()._format_body(self._details(), "TOK", "en")
        assert "New shift available: Ball Arena" in body
        assert "Date: Fri 18 Sep 2026" in body

    def test_broadcast_uses_each_workers_own_language(self, app, seed, monkeypatch):
        """Two workers, two languages, one dispatch."""
        from utils.sms import WhatsAppService

        employees = Employee.query.order_by(Employee.id).all()
        employees[0].language = "es"
        employees[1].language = "en"
        db.session.commit()

        bodies: list[str] = []
        service = WhatsAppService()
        monkeypatch.setattr(
            service, "_send", lambda to, **kw: bodies.append(kw.get("body")) or True
        )
        service.send_shift_broadcast(
            ShiftAssignment.query.all(), self._details()
        )

        assert any("Nuevo turno disponible" in b for b in bodies)
        assert any("New shift available" in b for b in bodies)


# --------------------------------------------------------------------------- #
# Catalog hygiene — these fail loudly when a string is added carelessly
# --------------------------------------------------------------------------- #
class TestCatalogIntegrity:
    def _spanish(self):
        return CATALOG["es"]

    def test_every_template_string_has_spanish(self):
        """A t("...") added to a template without a translation fails here."""
        import glob
        import io

        pattern = re.compile(TEMPLATE_T_CALL, re.S)
        missing = set()
        for path in glob.glob("templates/**/*.html", recursive=True):
            source = io.open(path, encoding="utf-8").read()
            for _quote, key in pattern.findall(source):
                if key and key not in self._spanish():
                    missing.add(key)
        assert not missing, f"untranslated strings: {sorted(missing)}"

    def test_placeholders_survive_translation(self):
        """{name} dropped or renamed in Spanish would raise at render time."""
        fields = lambda s: sorted(re.findall(r"\{(\w+)\}", s))
        mismatched = {
            key: value
            for key, value in self._spanish().items()
            if fields(key) != fields(value)
        }
        assert not mismatched, f"placeholder mismatch: {mismatched}"

    def test_translation_is_never_empty(self):
        blank = [k for k, v in self._spanish().items() if not v.strip()]
        assert not blank, f"blank translations: {blank}"

    def test_unknown_key_falls_back_to_english(self):
        assert translate("Not in the catalog", "es") == "Not in the catalog"

    def test_unknown_language_falls_back_to_english(self):
        assert translate("CHECK IN", "de") == "CHECK IN"

    def test_supported_languages_are_labelled_in_their_own_language(self):
        assert SUPPORTED_LANGUAGES == {"en": "English", "es": "Español"}

    def test_a_broken_translation_cannot_crash_a_page(self, monkeypatch):
        """A bad placeholder must degrade to English, not 500 a check-in."""
        monkeypatch.setitem(CATALOG["es"], "Hi {name}", "Hola {nombre}")
        assert translate("Hi {name}", "es", name="Maria") == "Hi Maria"

    def test_no_html_entities_in_translation_keys(self):
        """A key holding "&amp;" gets escaped a second time by Jinja and the
        page shows a literal "&amp;" to the user."""
        import glob
        import io as _io
        import re as _re

        pattern = _re.compile(TEMPLATE_T_CALL, _re.S)
        offenders = {}
        for path in glob.glob("templates/**/*.html", recursive=True):
            source = _io.open(path, encoding="utf-8").read()
            for _quote, key in pattern.findall(source):
                if _re.search(r"&(amp|lt|gt|quot|#\d+);", key):
                    offenders.setdefault(path, []).append(key)
        assert not offenders, f"HTML entities inside t() keys: {offenders}"

    def test_no_html_tags_in_translations(self):
        """Markup inside a translation would be escaped and shown as text."""
        import re as _re

        offenders = {
            k: v for k, v in self._spanish().items() if _re.search(r"<[a-zA-Z/]", v)
        }
        assert not offenders, f"markup inside translations: {offenders}"



class TestClientSideStrings:
    """Strings built in JavaScript must be translated too.

    These are easy to miss: the page looks fully Spanish until someone types
    in the search box or opens the date picker. The date picker only renders
    when at least one Job exists, hence the fixture.
    """

    def test_shifts_page_ships_a_spanish_string_table(self, admin_client, venue_job):
        page = admin_client.get("/admin/shifts?lang=es").get_data(as_text=True)
        assert "Crear {n} turnos" in page
        assert "septiembre" in page          # calendar month names
        assert "No hay turnos que coincidan" in page

    def test_string_table_is_declared_before_every_use(self, admin_client, venue_job):
        """It was scoped inside the date-picker IIFE, so the schedule-filter
        block threw 'T is not defined' as soon as anyone searched."""
        page = admin_client.get("/admin/shifts").get_data(as_text=True)
        declaration = page.index("const T = {")
        for marker in ("T.months", "T.createShift", "T.shiftsShown", "T.noMatches"):
            assert page.index(marker) > declaration, f"{marker} used before T is declared"
        assert page.count("const T = {") == 1, "T declared more than once"

    def test_calendar_does_not_use_the_device_locale(self, admin_client, venue_job):
        """toLocaleDateString would label the calendar in the laptop's language."""
        page = admin_client.get("/admin/shifts").get_data(as_text=True)
        assert "toLocaleDateString" not in page

    def test_punch_keypad_ships_its_own_table(self, client, make_assignment, worker):
        worker.language = "es"
        db.session.commit()
        assignment = make_assignment(start_in=timedelta(minutes=-5))
        assignment.punch_token = "jstok123"
        db.session.commit()

        page = client.get("/punch/jstok123").get_data(as_text=True)
        # tojson escapes non-ASCII, so assert on plain-ASCII Spanish.
        assert "Hable con su supervisor" in page
        assert "PIN INCORRECTO" in page
