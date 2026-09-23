"""Shared same-site redirect validation.

Both the admin login (``?next=``) and the language toggle (``?next=``) send a
browser back to where it came from, so both need the same answer to "is this
target somewhere on our own site?". Keeping one implementation means a fix
here covers every caller.
"""
from __future__ import annotations

from urllib.parse import unquote


def safe_relative_path(target: str | None) -> str | None:
    r"""Return ``target`` if it is a same-site relative path, else ``None``.

    Decoded before checking so percent-encoded attempts (``%2f%2fevil.com``)
    are judged on what the browser will actually resolve. Backslashes are
    rejected because some browsers normalize them to ``/``, turning
    ``/\evil.com`` into a protocol-relative URL. Control characters are
    rejected because they can be stripped mid-parse to reveal a new target.
    """
    decoded = unquote(target or "")
    if (
        decoded.startswith("/")
        and not decoded.startswith("//")
        and "\\" not in decoded
        and not any(ord(c) < 32 for c in decoded)
    ):
        return target
    return None
