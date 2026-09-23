"""Keep SMS bodies inside the GSM-7 alphabet.

A text message is billed per *segment*. In the GSM-7 alphabet a segment holds
153 characters; a single character outside it forces the whole message to
UCS-2, where a segment holds 67. One stray typographic character therefore
costs real money on every message, and eats into the Sole Proprietor brand's
daily segment cap.

GSM-7 does contain the Spanish characters ``ñ ü ¿ ¡ é``, but *not* ``á í ó ú``
— which is why SMS copy is authored without those, while the web pages keep
correct accented Spanish (encoding is irrelevant there).

This module only rewrites *punctuation* that has an unambiguous ASCII
equivalent. It never strips accents: silently turning "años" into "anos"
would be worse than paying for the extra segment.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# GSM 03.38 basic character set plus the escape-extension characters. The
# extension characters cost two septets each, which ``segment_count`` accounts
# for.
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = set("^{}\\[~]|€")

# Typographic characters that routinely sneak in from copy, templates and
# date formatting. Each maps to an ASCII equivalent that reads the same.
_SUBSTITUTIONS = {
    "–": "-",    # – en dash (shift time ranges)
    "—": "-",    # — em dash
    "‒": "-",    # ‒ figure dash
    "−": "-",    # − minus sign
    "‘": "'",    # ‘
    "’": "'",    # ’
    "‚": "'",    # ‚
    "“": '"',    # “
    "”": '"',    # ”
    "…": "...",  # …
    " ": " ",    # non-breaking space
    " ": " ",    # narrow no-break space
    " ": " ",    # thin space
    "·": ".",    # · middle dot
    "•": "-",    # • bullet
    "→": "->",   # →
    "­": "",     # soft hyphen (invisible, but not GSM-7)
}


def is_gsm7(text: str) -> bool:
    return all(c in GSM7_BASIC or c in GSM7_EXTENDED for c in text)


def non_gsm7_characters(text: str) -> set[str]:
    """The characters that would force this message to UCS-2."""
    return {c for c in text if c not in GSM7_BASIC and c not in GSM7_EXTENDED}


def segment_count(text: str) -> int:
    """Billable segments for ``text``, matching how carriers count them."""
    if is_gsm7(text):
        septets = sum(2 if c in GSM7_EXTENDED else 1 for c in text)
        return 1 if septets <= 160 else -(-septets // 153)
    # UCS-2 counts UTF-16 code units, so astral characters (emoji) cost two.
    units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    return 1 if units <= 70 else -(-units // 67)


def to_gsm7(text: str) -> str:
    """Replace typographic punctuation with GSM-7 equivalents.

    Anything still outside the alphabet afterwards — an accent that belongs in
    the word, an emoji — is left alone and logged, so the message goes out
    correct-but-costlier rather than mangled.
    """
    if not text:
        return text
    cleaned = text.translate(str.maketrans(_SUBSTITUTIONS))
    remaining = non_gsm7_characters(cleaned)
    if remaining:
        logger.info(
            "SMS body still needs UCS-2 (%d segments) because of %s",
            segment_count(cleaned),
            "".join(sorted(remaining)),
        )
    return cleaned
