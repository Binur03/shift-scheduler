"""Worker check-in PINs: generation, keyed hashing, verification.

A 4-digit PIN has only 10,000 values, so an unkeyed hash (even bcrypt) is
reversed by trying them all. PINs are instead stored as

    HMAC-SHA256(PIN_PEPPER, "<employee_id>:<pin>")

where PIN_PEPPER lives in Secret Manager, never in the database. A leaked
DB dump or backup therefore reveals nothing usable. The employee id in the
message means two workers with the same PIN still get different hashes.
Online guessing is stopped separately by the per-link lockout.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

from models import Employee, utcnow_naive

PIN_RE = re.compile(r"^\d{4}$")

# Easily guessed PINs are never issued.
_WEAK_PINS = (
    {str(d) * 4 for d in range(10)}                                  # 0000, 1111 ...
    | {"".join(str((s + i) % 10) for i in range(4)) for s in range(10)}   # 0123, 1234 ... 9012
    | {"".join(str((s - i) % 10) for i in range(4)) for s in range(10)}   # 3210, 4321 ...
    | {"1212", "2121", "1122", "6969", "1004", "2000", "2580", "0852"}
)


class PinConfigError(RuntimeError):
    """PIN_PEPPER is not configured; PIN features must fail closed."""


def _pepper() -> bytes:
    pepper = os.environ.get("PIN_PEPPER", "")
    if len(pepper) < 16:
        raise PinConfigError("PIN_PEPPER is missing or too short (need 16+ chars).")
    return pepper.encode()


def pin_configured() -> bool:
    try:
        _pepper()
        return True
    except PinConfigError:
        return False


def is_valid_pin_format(pin: object) -> bool:
    return isinstance(pin, str) and bool(PIN_RE.match(pin))


def generate_pin() -> str:
    """Random 4-digit PIN from a CSPRNG, excluding trivially guessable ones."""
    while True:
        pin = f"{secrets.randbelow(10_000):04d}"
        if pin not in _WEAK_PINS:
            return pin


def hash_pin(employee_id: int, pin: str) -> str:
    return hmac.new(_pepper(), f"{employee_id}:{pin}".encode(), hashlib.sha256).hexdigest()


def set_new_pin(employee: Employee) -> str:
    """Issue a fresh PIN for ``employee`` (caller commits). Returns the PIN,
    which must be shown to the admin once and never stored or logged."""
    if employee.id is None:
        raise ValueError("employee must be flushed (have an id) before setting a PIN")
    pin = generate_pin()
    employee.pin_hash = hash_pin(employee.id, pin)
    employee.pin_set_at_utc = utcnow_naive()
    return pin


def verify_pin(employee: Employee, pin: str) -> bool:
    """Constant-time check. Always computes an HMAC — even with no PIN set —
    so response timing doesn't reveal whether a worker has a PIN."""
    candidate = hash_pin(employee.id, pin if is_valid_pin_format(pin) else "0000")
    stored = employee.pin_hash or ("0" * 64)
    matches = hmac.compare_digest(candidate, stored)
    return matches and employee.pin_hash is not None and is_valid_pin_format(pin)
