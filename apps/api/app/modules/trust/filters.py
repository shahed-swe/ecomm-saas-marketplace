"""Contact-exfiltration filter for buyer↔vendor chat.

Why this exists: a marketplace that lets a vendor say "call me on 01712345678 and pay bKash" loses
the order, the buyer protection and the commission at once — and the buyer loses every recourse.
So contact details are redacted in the message body and the attempt is recorded, rather than the
message being silently dropped: silent drops teach people to try harder, a visible redaction
teaches them the rule.

Bangla digits are normalised first, because ০১৭১২৩৪৫৬৭৮ is a phone number too.
"""

import hashlib
import re

BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
SEPARATORS = re.compile(r"[\s\-\.\(\)_/]+")
# 11-digit BD mobile numbers, with or without +88, however they are spaced out
PHONE = re.compile(r"(?:\+?88)?0?1[3-9]\d{8}")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.(?:com|net|org|info|bd|xyz|shop|store|me|io)(?:\.bd)?\b",
    re.I,
)
SOCIAL = re.compile(r"\b(?:whats\s*app|whatsapp|imo|telegram|messenger|facebook|fb|viber)\b", re.I)
OFF_PLATFORM = re.compile(
    r"\b(?:bkash|nagad|rocket|send\s*money|cash\s*out|personal\s*number|direct\s*(?:deal|payment))\b",
    re.I,
)
REDACTION = "[hidden]"


def _digits_view(text: str) -> str:
    return SEPARATORS.sub("", text.translate(BANGLA_DIGITS))


def scan(text: str) -> list[str]:
    """Which rules this message trips, in the order a human would explain them."""
    flags = []
    if PHONE.search(_digits_view(text)):
        flags.append("phone")
    if EMAIL.search(text):
        flags.append("email")
    if URL.search(text):
        flags.append("link")
    if SOCIAL.search(text):
        flags.append("social_handle")
    if OFF_PLATFORM.search(text):
        flags.append("off_platform_payment")
    return flags


def redact(text: str) -> tuple[str, list[str], bool]:
    """Returns the message as it will be stored, the flags it tripped, and whether it was changed.

    Only contact details are removed; the rest of what the person wrote is left exactly as typed,
    so the conversation still reads naturally and support can see what was meant.
    """
    flags = scan(text)
    if not flags:
        return text, [], False
    cleaned = EMAIL.sub(REDACTION, text)
    cleaned = URL.sub(REDACTION, cleaned)
    cleaned = _redact_phones(cleaned)
    return cleaned, flags, cleaned != text or "phone" in flags


def _redact_phones(text: str) -> str:
    """Phone numbers survive spacing tricks (`017 12 345 678`), so match on a digits-only view."""
    normalised = text.translate(BANGLA_DIGITS)
    out, i = [], 0
    while i < len(normalised):
        ch = normalised[i]
        if not ch.isdigit() and ch not in "+":
            out.append(ch)
            i += 1
            continue
        j, digits, last_digit = i, [], i
        while j < len(normalised) and (normalised[j].isdigit() or normalised[j] in "+ -.()_"):
            if normalised[j].isdigit():
                digits.append(normalised[j])
                last_digit = j
            j += 1
        j = last_digit + 1  # never swallow the spacing that follows the number
        run = "".join(digits)
        if PHONE.fullmatch(run) or (len(run) >= 11 and PHONE.search(run)):
            out.append(REDACTION)
        else:
            out.append(normalised[i:j])
        i = j
    return "".join(out)


def fingerprint(text: str) -> str:
    """A hash of what was originally typed: proof for a moderator, without storing the number."""
    return hashlib.sha256(text.encode()).hexdigest()
