"""Administrative CLI utilities.

Registers a ``flask admin`` command group. The headline command bulk-imports
workers from a CSV so a business owner can seed their roster in one step:

    flask admin import-workers ./workers.csv

The CSV must have a header row with columns: ``first_name``, ``last_name``,
``phone_number``. Phone numbers are normalized to strict E.164
(e.g. ``+13035550123``); rows that cannot be normalized or that duplicate an
existing/within-file number are skipped and reported rather than aborting the
whole import.
"""
from __future__ import annotations

import csv
import logging
import os
import re

import click
from flask import Flask
from flask.cli import AppGroup
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import Employee

logger = logging.getLogger(__name__)

# E.164: a leading '+' followed by 8-15 digits, first digit non-zero.
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

REQUIRED_COLUMNS = ("first_name", "last_name", "phone_number")


def normalize_e164(raw: str, default_country_code: str = "1") -> str:
    """Normalize a raw phone string to E.164 (``+<digits>``).

    Rules:
      * An explicit leading ``+`` is trusted; only its digits are kept.
      * A 10-digit national number is prefixed with ``default_country_code``.
      * An 11-digit number already starting with the country code gets a ``+``.
      * Anything else is assembled best-effort and then strictly validated.

    Args:
        raw: the source string, possibly containing spaces, dashes, parens.
        default_country_code: country code (no ``+``) for national numbers.

    Returns:
        A validated E.164 string.

    Raises:
        ValueError: if the result is not valid E.164.
    """
    if raw is None:
        raise ValueError("empty phone number")

    stripped = raw.strip()
    if not stripped:
        raise ValueError("empty phone number")

    if stripped.startswith("+"):
        digits = re.sub(r"\D", "", stripped[1:])
        candidate = f"+{digits}"
    else:
        digits = re.sub(r"\D", "", stripped)
        if len(digits) == 10:
            candidate = f"+{default_country_code}{digits}"
        elif len(digits) == 11 and digits.startswith(default_country_code):
            candidate = f"+{digits}"
        else:
            candidate = f"+{digits}"

    if not _E164_RE.match(candidate):
        raise ValueError(f"{raw!r} does not normalize to valid E.164 ({candidate!r})")
    return candidate


admin_cli = AppGroup("admin", help="Administrative utilities for shift-scheduler.")


@admin_cli.command("import-workers")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--country-code",
    default=lambda: os.environ.get("DEFAULT_COUNTRY_CODE", "1"),
    show_default="env DEFAULT_COUNTRY_CODE or '1'",
    help="Default country code for national (no '+') numbers.",
)
def import_workers(csv_path: str, country_code: str) -> None:
    """Bulk-import employees from CSV at CSV_PATH."""
    inserted = 0
    skipped = 0

    # Pre-load existing phone numbers to dedupe against the live table.
    existing_phones: set[str] = {
        phone for (phone,) in db.session.query(Employee.phone_number).all()
    }
    seen_in_file: set[str] = set()
    to_insert: list[dict[str, object]] = []

    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)

        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise click.ClickException(
                f"CSV is missing required column(s): {', '.join(missing)}"
            )

        for line_no, row in enumerate(reader, start=2):  # row 1 is the header
            first_name = (row.get("first_name") or "").strip()
            last_name = (row.get("last_name") or "").strip()
            raw_phone = (row.get("phone_number") or "").strip()

            if not (first_name and last_name and raw_phone):
                skipped += 1
                logger.warning("Row %d skipped: missing required field(s).", line_no)
                continue

            try:
                phone = normalize_e164(raw_phone, default_country_code=country_code)
            except ValueError as exc:
                skipped += 1
                logger.warning("Row %d skipped: %s", line_no, exc)
                continue

            if phone in existing_phones or phone in seen_in_file:
                skipped += 1
                logger.info("Row %d skipped: duplicate phone %s.", line_no, phone)
                continue

            seen_in_file.add(phone)
            to_insert.append(
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "phone_number": phone,
                    "is_active": True,
                }
            )

    if not to_insert:
        click.echo(f"No new workers to import. Skipped {skipped} row(s).")
        return

    try:
        # Fast path: single multi-row INSERT.
        db.session.bulk_insert_mappings(Employee, to_insert)
        db.session.commit()
        inserted = len(to_insert)
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Bulk insert failed; no workers were imported.")
        raise click.ClickException(
            "Bulk insert failed (see logs). Transaction rolled back."
        )

    click.echo(f"Imported {inserted} worker(s); skipped {skipped} row(s).")


@admin_cli.command("check-staffing")
def check_staffing() -> None:
    """Run the 24-48h understaffing sweep and alert the admin via WhatsApp."""
    from utils.alerts import check_understaffed_shifts

    result = check_understaffed_shifts()
    click.echo(
        f"Window {result.checked_window}: {result.understaffed} understaffed, "
        f"{result.alerted} alerted."
    )


@admin_cli.command("issue-pins")
@click.option("--all", "reissue_all", is_flag=True, help="Re-issue PINs for workers who already have one.")
def issue_pins(reissue_all: bool) -> None:
    """Issue check-in PINs to active workers (default: only those without one).

    Prints name, phone, PIN as CSV to stdout exactly once — hand these out
    privately; PINs are stored only as keyed hashes and can't be recovered.
    """
    from utils.pins import PinConfigError, set_new_pin

    query = Employee.query.filter_by(is_active=True)
    if not reissue_all:
        query = query.filter(Employee.pin_hash.is_(None))
    employees = query.order_by(Employee.last_name, Employee.first_name).all()
    if not employees:
        click.echo("No workers need a PIN.", err=True)
        return

    writer = csv.writer(click.get_text_stream("stdout"))
    writer.writerow(["name", "phone_number", "pin"])
    try:
        rows = [(e.full_name, e.phone_number, set_new_pin(e)) for e in employees]
        db.session.commit()
    except PinConfigError as exc:
        db.session.rollback()
        raise click.ClickException(str(exc))
    for row in rows:
        writer.writerow(row)
    click.echo(f"Issued {len(rows)} PIN(s).", err=True)


def register_cli(app: Flask) -> None:
    """Attach the ``admin`` command group to the Flask app."""
    app.cli.add_command(admin_cli)
